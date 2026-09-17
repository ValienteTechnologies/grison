"""BookStack wiki pages, on the sync engine (ENGINE.md Adapter protocol; BRIEF D4).

A page's book/chapter come from its directory only (D4) — the local document
(:mod:`grison.formats.wiki`) carries just ``title``/``priority``/``tags``/body; this
module attaches ``book``/``chapter`` (directory-derived slugs) when it scans a file, so
:meth:`BsPageAdapter.canonical_local` has everything it needs without a path argument
(the :class:`~grison.engine.adapter.Adapter` protocol doesn't pass one).

No converter: BookStack pages are markdown-native, mirrored verbatim (same as the
pre-engine module this replaces).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._bs_common import BSContext, slugify
from grison.engine.adapter import AdapterMode
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto, VetoSeverity
from grison.formats import wiki as wiki_fmt
from grison.formats.common import FormatError
from grison.remote.bookstack import BookStackError

_RECYCLE_REASON = "in the BookStack recycle bin, recoverable there"


@dataclass(frozen=True)
class PageDoc:
    """A parsed local page plus its directory-derived parent (D4) — the object
    :class:`grison.engine.model.LocalDoc.doc` holds for kind ``bs.page``."""

    title: str
    priority: int | None
    tags: list[str]
    body: str
    book: str
    chapter: str | None


def _tags_to_local(bs_tags: list[dict[str, Any]]) -> list[str]:
    """BookStack tag ``{"name","value"}`` -> a v2 frontmatter string. A value-less tag
    round-trips as its bare name; a valued tag (v2's ``tags`` is a plain string list —
    :mod:`grison.formats.wiki` has no name/value structure) serializes as
    ``"name:value"`` — a documented, reversible convention, not a BookStack format."""
    out = []
    for t in bs_tags:
        name = str(t.get("name") or "")
        value = str(t.get("value") or "")
        out.append(f"{name}:{value}" if value else name)
    return out


def _tags_to_remote(local_tags: list[str]) -> list[dict[str, Any]]:
    out = []
    for t in local_tags:
        name, sep, value = t.partition(":")
        out.append({"name": name, "value": value} if sep else {"name": t, "value": ""})
    return out


def _normalize(
    detail: dict[str, Any], books_by_id: dict[int, dict[str, Any]],
    chapters_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    book_id = detail["book_id"]
    chapter_id = detail.get("chapter_id") or 0
    return {
        "id": detail["id"],
        "name": detail["name"],
        "book_id": book_id,
        "book_slug": books_by_id.get(book_id, {}).get("slug", "book"),
        "chapter_id": chapter_id or None,
        "chapter_slug": chapters_by_id.get(chapter_id, {}).get("slug") if chapter_id else None,
        "priority": detail.get("priority"),
        "tags": detail.get("tags") or [],
        "markdown": detail.get("markdown") or "",
        "html": detail.get("html") or "",
        "raw_html": detail.get("raw_html") or "",
        "editor": detail.get("editor"),
        "draft": bool(detail.get("draft", False)),
        "template": bool(detail.get("template", False)),
        "updated_at": detail.get("updated_at"),
        "revision_count": detail.get("revision_count"),
    }


class BsPageAdapter:
    kind = "bs.page"
    mode: AdapterMode = "read-write"

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "methodology" / "library"
        if not base.is_dir():
            return
        for book_dir in sorted(p for p in base.iterdir() if p.is_dir() and p.name != ".shelves"):
            yield from self._scan_dir(root, book_dir, book_dir.name, None)
            for chapter_dir in sorted(
                p for p in book_dir.iterdir() if p.is_dir() and p.name != "images"
            ):
                yield from self._scan_dir(root, chapter_dir, book_dir.name, chapter_dir.name)

    def _scan_dir(
        self, root: Path, dir_: Path, book: str, chapter: str | None
    ) -> Iterable[LocalDoc]:
        for md in sorted(dir_.glob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            rel = PurePosixPath(md.relative_to(root).as_posix())
            raw = md.read_text(encoding="utf-8")
            doc: PageDoc | None
            try:
                wdoc = wiki_fmt.parse(raw, path=md)
                doc = PageDoc(title=wdoc.title, priority=wdoc.priority, tags=list(wdoc.tags),
                              body=wdoc.body, book=book, chapter=chapter)
            except FormatError:
                doc = None
            yield LocalDoc(path=rel, doc=doc, raw_text=raw)

    def fetch_remote(self, ctx: BSContext) -> dict[int, RemoteRecord]:
        assert isinstance(ctx, BSContext) and ctx.state is not None
        rows = ctx.client.fetch_pages()
        books_by_id = ctx.books_by_id
        chapters_by_id = ctx.chapters_by_id
        out: dict[int, RemoteRecord] = {}
        for row in rows:
            pid = row["id"]
            witness = {"updated_at": row.get("updated_at"),
                      "revision_count": row.get("revision_count")}
            cached = ctx.state.get(self.kind, pid)
            if cached is not None and cached.witness == witness and cached.base is not None:
                out[pid] = RemoteRecord(
                    id=pid,
                    data={"_skipped": True, "id": pid, "book_id": row["book_id"],
                         "chapter_id": row.get("chapter_id")},
                    witness=witness, cached_hash=cached.base,
                )
                continue
            detail = ctx.client.fetch_page(pid)
            out[pid] = RemoteRecord(id=pid, data=_normalize(detail, books_by_id, chapters_by_id),
                                    witness=witness)
        # Recycle-bin awareness (BRIEF B) is scoped to records grison already knows
        # about — an indexed id whose remote copy is now in the bin gets the SKIP
        # below; a binned page grison has never indexed (someone else's housekeeping,
        # or one this workspace never pulled) is none of grison's business and must
        # be ignored entirely: not fetched into a Plan, not counted, no event, not
        # even on every sync forever (the bug this guard closes).
        indexed_ids = ctx.indexed_page_ids
        for entry in ctx.client.fetch_recycle_bin():
            if entry.get("deletable_type") != "page":
                continue
            rid = entry.get("deletable_id")
            if rid is None or rid in out or rid not in indexed_ids:
                continue
            deletable = entry.get("deletable") or {}
            out[rid] = RemoteRecord(
                id=rid, data={"_recycled": True, "id": rid, "name": deletable.get("name", "")},
                witness={},
            )
        return out

    def refetch(self, ctx: BSContext, id: int) -> RemoteRecord | None:
        try:
            detail = ctx.client.fetch_page(id)
        except BookStackError:
            return None
        return RemoteRecord(id=id, data=_normalize(detail, ctx.books_by_id, ctx.chapters_by_id),
                            witness={"updated_at": detail.get("updated_at"),
                                    "revision_count": detail.get("revision_count")})

    def canonical_local(self, doc: PageDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        return {"title": doc.title, "priority": doc.priority, "tags": doc.tags,
                "body": doc.body, "book": doc.book, "chapter": doc.chapter}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        if data.get("_recycled") or data.get("_skipped"):
            # never hashed for a real comparison: _recycled is always vetoed to SKIP
            # before a write; _skipped supplies cached_hash instead of calling this.
            return {"_unavailable": True}
        return {"title": data["name"], "priority": data.get("priority"),
                "tags": _tags_to_local(data.get("tags") or []),
                "body": (data.get("markdown") or "").strip(),
                "book": data["book_slug"], "chapter": data.get("chapter_slug")}

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        doc = wiki_fmt.WikiPageDoc(
            title=data["name"], priority=data.get("priority"),
            tags=_tags_to_local(data.get("tags") or []), body=(data.get("markdown") or "").strip(),
        )
        return wiki_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        parts = ["methodology", "library", data["book_slug"]]
        if data.get("chapter_slug"):
            parts.append(data["chapter_slug"])
        parts.append(f"{slugify(data['name'])}.md")
        return PurePosixPath(*parts)

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        parts = ["methodology", "library", data["book_slug"]]
        if data.get("chapter_slug"):
            parts.append(data["chapter_slug"])
        parts.append(current.name)
        return PurePosixPath(*parts)

    def _resolve_parent(self, ctx: BSContext, doc: PageDoc) -> tuple[int | None, int | None]:
        book = ctx.books_by_slug.get(doc.book)
        if book is None:
            raise LookupError(f"unknown book {doc.book!r} — create the book directory first")
        if doc.chapter is None:
            return book["id"], None
        chapter = ctx.chapters_by_book_slug.get((book["id"], doc.chapter))
        if chapter is None:
            raise LookupError(f"unknown chapter {doc.chapter!r} in book {doc.book!r}")
        return None, chapter["id"]

    def create(self, ctx: BSContext, doc: PageDoc) -> RemoteRecord:
        assert isinstance(ctx, BSContext)
        book_id, chapter_id = self._resolve_parent(ctx, doc)
        rec = ctx.client.create_page(
            name=doc.title, markdown=doc.body, book_id=book_id, chapter_id=chapter_id,
            tags=_tags_to_remote(doc.tags), priority=doc.priority,
        )
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        return RemoteRecord(id=rec["id"], data=data,
                            witness={"updated_at": rec.get("updated_at"),
                                    "revision_count": rec.get("revision_count")})

    def update(self, ctx: BSContext, id: int, doc: PageDoc) -> RemoteRecord:
        assert isinstance(ctx, BSContext)
        book_id, chapter_id = self._resolve_parent(ctx, doc)
        rec = ctx.client.update_page(
            id, markdown=doc.body, name=doc.title, book_id=book_id, chapter_id=chapter_id,
            priority=doc.priority, tags=_tags_to_remote(doc.tags),
        ) or ctx.client.fetch_page(id)
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        return RemoteRecord(id=id, data=data,
                            witness={"updated_at": rec.get("updated_at"),
                                    "revision_count": rec.get("revision_count")})

    def delete(self, ctx: BSContext, id: int) -> None:
        assert isinstance(ctx, BSContext)
        ctx.client.delete_page(id)

    def restore(self, ctx: BSContext, preimage: Any) -> RemoteRecord:
        """Undo's inverse: recreate (the id is gone once deleted — BookStack does not
        offer a page-restore-by-id call over the plain REST API) or, if the id still
        exists, PUT the pre-image straight back."""
        assert isinstance(ctx, BSContext)
        if preimage is None:
            raise ValueError("no pre-image recorded — cannot restore")
        pid = preimage.get("id")
        book_id = preimage.get("book_id")
        chapter_id = preimage.get("chapter_id")
        markdown = preimage.get("markdown") or ""
        name = preimage.get("name") or "Untitled"
        tags = preimage.get("tags") or []
        priority = preimage.get("priority")
        existing = None
        if pid is not None:
            try:
                existing = ctx.client.fetch_page(pid)
            except BookStackError:
                existing = None
        if existing is not None:
            rec = ctx.client.update_page(pid, markdown=markdown, name=name, book_id=book_id,
                                         chapter_id=chapter_id, priority=priority,
                                         tags=tags) or ctx.client.fetch_page(pid)
        else:
            rec = ctx.client.create_page(name=name, markdown=markdown, book_id=book_id,
                                         chapter_id=chapter_id, tags=tags, priority=priority)
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        return RemoteRecord(id=rec["id"], data=data,
                            witness={"updated_at": rec.get("updated_at"),
                                    "revision_count": rec.get("revision_count")})

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        if not isinstance(remote, dict):
            return None
        if remote.get("_recycled"):
            # only ever reached for an INDEXED id (fetch_remote already filtered
            # unknown binned pages out entirely) — grison was tracking this record,
            # so its disappearance is worth the user's attention.
            return Veto(_RECYCLE_REASON, VetoSeverity.ATTENTION)
        if remote.get("_skipped"):
            return None
        editor = remote.get("editor")
        if editor and editor != "markdown":
            # a wysiwyg page grison has never managed (no local copy) is expected and
            # inert — nothing lost, nothing to do. One that blocks a pending local
            # edit (local is not None) is the case the user actually needs to see.
            severity = VetoSeverity.ATTENTION if local is not None else VetoSeverity.INFO
            return Veto(
                "remote page is not markdown-native (wysiwyg) — convert it in BookStack first",
                severity,
            )
        if remote.get("draft"):
            return Veto("remote page is a draft — grison never syncs drafts", VetoSeverity.INFO)
        if remote.get("template"):
            return Veto("remote page is a template — grison never syncs templates",
                        VetoSeverity.INFO)
        return None

    def remote_label(self, data: Any) -> str:
        if not isinstance(data, dict):
            return str(data)
        name = data.get("name") or "(untitled)"
        rid = data.get("id")
        return f'"{name}" (page {rid})' if rid is not None else f'"{name}"'
