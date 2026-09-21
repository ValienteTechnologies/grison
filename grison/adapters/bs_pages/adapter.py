"""``methodology/library/**/*.md`` <-> BookStack pages — see
:mod:`grison.adapters.bs_pages`'s own docstring for the gallery-translation shape."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._bs_common import BSContext, slugify
from grison.engine.adapter import AdapterMode
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto, VetoSeverity
from grison.formats import wiki as wiki_fmt
from grison.formats.common import FormatError
from grison.remote.bookstack import BookStackError

from .gallery import (
    _gallery_by_name_for_book,
    _local_body_to_remote,
    _localize_gallery_urls,
    _remote_body_for_push,
    _substitute_gallery_tokens,
)
from .normalize import PageDoc, _normalize, _tags_to_local, _tags_to_remote

_RECYCLE_REASON = "in the BookStack recycle bin, recoverable there"


@dataclass
class BsPageAdapter:
    """``methodology/library/**/*.md`` <-> BookStack pages.

    ``canonical_local``/``canonical_remote`` (the ``Adapter`` protocol's two
    ctx-less methods) replace every gallery image reference's PATH/URL with a
    :func:`~grison.adapters.bs_pages.gallery._gallery_token`
    (:func:`~grison.adapters.bs_pages.gallery._substitute_gallery_tokens`) before
    hashing — D9's counterpart to the id-folding :mod:`grison.adapters.gw_report`/
    :mod:`grison.adapters.gw_findings` do via :func:`grison.engine.filesets.
    canonical_prose`/``canonical_remote_prose``, adapted to D9's own vocabulary
    (a stored absolute gallery URL, not an html node id). ``canonical_local``
    tokenizes via the LIVE per-book gallery (whatever the CURRENT url/id for a
    filename is); ``canonical_remote`` tokenizes an ALREADY-resolved (this run)
    local-spelled reference the SAME way, but a still-raw, unresolved URL (one
    :func:`~grison.adapters.bs_pages.gallery._localize_gallery_urls` could not
    translate — an orphaned reference after a reupload) DIRECTLY, as a pure
    function of the URL TEXT itself,
    never through a live lookup (see :func:`~grison.adapters.bs_pages.gallery.
    _substitute_gallery_tokens`'s own docstring for the full reasoning,
    including why the SUBSTITUTION has to happen IN the body text, not as a
    separate parallel fold list alongside the unchanged raw text — a fold list
    alone still leaves the "body" field itself different between an authored
    ``images/x.png`` and BookStack's stored URL, which is enough on its own to
    make two otherwise-identical records hash differently): D9 ("replacing an
    image's bytes must re-push every page referencing it, automatically, in
    the same run") needs the page's remote canonical form to stay UNCHANGED
    across a reupload that left the page's own stored body untouched (still
    naming the OLD url, unresolved though it now is) — a live lookup on that
    orphaned URL instead drifted the remote payload itself off ``base``, and
    the page could only ever reach a COLLISION, needing ``--force-local``,
    never the automatic same-run PUSH D9 requires (see
    ``tests/e2e/test_wiki_images_e2e.py``).

    Resolving a LOCAL-spelled reference needs the live gallery (``ctx`` — the
    ``Adapter`` protocol doesn't hand ``canonical_local``/``canonical_remote``
    one at all), so ``self._ctx`` is captured as a side effect of
    ``fetch_remote``/``refetch``/``create``/``update`` — the only methods that
    DO receive ``ctx`` — mirroring :class:`grison.adapters.gw_report.
    NarrativeSectionAdapter`'s identical ``self._index`` caching (see that
    class's own docstring for why this is safe: :mod:`grison.engine.documents`'s
    loop order guarantees ``fetch_remote`` runs before any ``canonical_local``/
    ``canonical_remote`` call on the SAME adapter instance within one sync). The
    one caller with no ctx-bearing call at all (:func:`grison.engine.offline_status.
    compute_offline_status`) degrades to no substitution at all (the raw body),
    never a crash."""

    kind = "bs.page"
    mode: AdapterMode = "read-write"
    _ctx: BSContext | None = field(default=None, init=False, repr=False, compare=False)

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
                doc = PageDoc(
                    title=wdoc.title,
                    priority=wdoc.priority,
                    tags=list(wdoc.tags),
                    body=wdoc.body,
                    book=book,
                    chapter=chapter,
                )
            except FormatError:
                doc = None
            yield LocalDoc(path=rel, doc=doc, raw_text=raw)

    def fetch_remote(self, ctx: BSContext) -> dict[int, RemoteRecord]:
        assert isinstance(ctx, BSContext) and ctx.state is not None
        self._ctx = ctx
        rows = ctx.client.fetch_pages()
        books_by_id = ctx.books_by_id
        chapters_by_id = ctx.chapters_by_id
        out: dict[int, RemoteRecord] = {}
        for row in rows:
            pid = row["id"]
            witness = {
                "updated_at": row.get("updated_at"),
                "revision_count": row.get("revision_count"),
            }
            cached = ctx.state.get(self.kind, pid)
            if cached is not None and cached.witness == witness and cached.base is not None:
                out[pid] = RemoteRecord(
                    id=pid,
                    data={
                        "_skipped": True,
                        "id": pid,
                        "book_id": row["book_id"],
                        "chapter_id": row.get("chapter_id"),
                    },
                    witness=witness,
                    cached_hash=cached.base,
                )
                continue
            detail = ctx.client.fetch_page(pid)
            data = _normalize(detail, books_by_id, chapters_by_id)
            _localize_gallery_urls(ctx, data)
            out[pid] = RemoteRecord(id=pid, data=data, witness=witness)
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
                id=rid,
                data={"_recycled": True, "id": rid, "name": deletable.get("name", "")},
                witness={},
            )
        return out

    def refetch(self, ctx: BSContext, id: int) -> RemoteRecord | None:
        self._ctx = ctx
        try:
            detail = ctx.client.fetch_page(id)
        except BookStackError:
            return None
        data = _normalize(detail, ctx.books_by_id, ctx.chapters_by_id)
        _localize_gallery_urls(ctx, data)
        return RemoteRecord(
            id=id,
            data=data,
            witness={
                "updated_at": detail.get("updated_at"),
                "revision_count": detail.get("revision_count"),
            },
        )

    def canonical_local(self, doc: PageDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        book_id = self._ctx.books_by_slug.get(doc.book, {}).get("id") if self._ctx else None
        body = _substitute_gallery_tokens(self._ctx, doc.body, book_id=book_id)
        return {
            "title": doc.title,
            "priority": doc.priority,
            "tags": doc.tags,
            "body": body,
            "book": doc.book,
            "chapter": doc.chapter,
        }

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        if data.get("_recycled") or data.get("_skipped"):
            # never hashed for a real comparison: _recycled is always vetoed to SKIP
            # before a write; _skipped supplies cached_hash instead of calling this.
            return {"_unavailable": True}
        raw_body = (data.get("markdown") or "").strip()
        body = _substitute_gallery_tokens(self._ctx, raw_body, book_id=data.get("book_id"))
        return {
            "title": data["name"],
            "priority": data.get("priority"),
            "tags": _tags_to_local(data.get("tags") or []),
            "body": body,
            "book": data["book_slug"],
            "chapter": data.get("chapter_slug"),
        }

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        doc = wiki_fmt.WikiPageDoc(
            title=data["name"],
            priority=data.get("priority"),
            tags=_tags_to_local(data.get("tags") or []),
            body=(data.get("markdown") or "").strip(),
        )
        return wiki_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        parts = ["methodology", "library", data["book_slug"]]
        if data.get("chapter_slug"):
            parts.append(data["chapter_slug"])
        # BookStack's OWN page slug, exactly like book/chapter directories use the
        # remote slug: it is what every internal link (`/books/<b>/page/<slug>`,
        # WIKI-007) and every wiki URL already names, so the file resolves them by
        # construction. Passed through slugify because a BookStack collision suffix
        # is mixed-case (`notes-aBc`) and file names are lower-case only (WS-001);
        # WIKI-007 matches case-insensitively for the same reason. slugify(name)
        # is only the fallback for a row without a slug.
        parts.append(f"{slugify(data.get('slug') or data['name'])}.md")
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
        self._ctx = ctx
        book_id, chapter_id = self._resolve_parent(ctx, doc)
        if book_id is not None:
            real_book_id = book_id
        else:
            assert chapter_id is not None
            real_book_id = ctx.chapters_by_id[chapter_id]["book_id"]
        rec = ctx.client.create_page(
            name=doc.title,
            markdown=_remote_body_for_push(ctx, doc, real_book_id),
            book_id=book_id,
            chapter_id=chapter_id,
            tags=_tags_to_remote(doc.tags),
            priority=doc.priority,
        )
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        _localize_gallery_urls(ctx, data)
        return RemoteRecord(
            id=rec["id"],
            data=data,
            witness={
                "updated_at": rec.get("updated_at"),
                "revision_count": rec.get("revision_count"),
            },
        )

    def update(self, ctx: BSContext, id: int, doc: PageDoc) -> RemoteRecord:
        assert isinstance(ctx, BSContext)
        self._ctx = ctx
        book_id, chapter_id = self._resolve_parent(ctx, doc)
        if book_id is not None:
            real_book_id = book_id
        else:
            assert chapter_id is not None
            real_book_id = ctx.chapters_by_id[chapter_id]["book_id"]
        rec = ctx.client.update_page(
            id,
            markdown=_remote_body_for_push(ctx, doc, real_book_id),
            name=doc.title,
            book_id=book_id,
            chapter_id=chapter_id,
            priority=doc.priority,
            tags=_tags_to_remote(doc.tags),
        ) or ctx.client.fetch_page(id)
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        _localize_gallery_urls(ctx, data)
        return RemoteRecord(
            id=id,
            data=data,
            witness={
                "updated_at": rec.get("updated_at"),
                "revision_count": rec.get("revision_count"),
            },
        )

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
        if book_id is not None and "images/" in markdown:
            # the preimage's markdown was captured already LOCALIZED (fetch_remote/
            # refetch/create/update all localize `data["markdown"]` in place — see
            # package docstring) — translate back to the URL form BookStack itself
            # stores before sending it back.
            markdown = _local_body_to_remote(markdown, _gallery_by_name_for_book(ctx, book_id))
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
            rec = ctx.client.update_page(
                pid,
                markdown=markdown,
                name=name,
                book_id=book_id,
                chapter_id=chapter_id,
                priority=priority,
                tags=tags,
            ) or ctx.client.fetch_page(pid)
        else:
            rec = ctx.client.create_page(
                name=name,
                markdown=markdown,
                book_id=book_id,
                chapter_id=chapter_id,
                tags=tags,
                priority=priority,
            )
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        return RemoteRecord(
            id=rec["id"],
            data=data,
            witness={
                "updated_at": rec.get("updated_at"),
                "revision_count": rec.get("revision_count"),
            },
        )

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
            return Veto(
                "remote page is a template — grison never syncs templates", VetoSeverity.INFO
            )
        return None

    def remote_label(self, data: Any) -> str:
        if not isinstance(data, dict):
            return str(data)
        name = data.get("name") or "(untitled)"
        rid = data.get("id")
        return f'"{name}" (page {rid})' if rid is not None else f'"{name}"'
