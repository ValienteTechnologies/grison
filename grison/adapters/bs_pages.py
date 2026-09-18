"""BookStack wiki pages, on the sync engine (ENGINE.md Adapter protocol; BRIEF D4).

A page's book/chapter come from its directory only (D4) — the local document
(:mod:`grison.formats.wiki`) carries just ``title``/``priority``/``tags``/body; this
module attaches ``book``/``chapter`` (directory-derived slugs) when it scans a file, so
:meth:`BsPageAdapter.canonical_local` has everything it needs without a path argument
(the :class:`~grison.engine.adapter.Adapter` protocol doesn't pass one).

No converter for prose: BookStack pages are markdown-native, mirrored verbatim (same
as the pre-engine module this replaces). ONE exception (D9/BRIEF task C): a gallery
image line, ``![caption](images/x.png)`` at the book root or ``![caption](../images/
x.png)`` inside a chapter (REF-007's two accepted spellings), is translated to/from
BookStack's own absolute gallery URL — the only spelling that actually renders an
image in BookStack, and the one it stores verbatim in ``markdown``. The translation
happens ONLY where a live gallery lookup (``ctx``) is available:
:meth:`fetch_remote`/:meth:`refetch` translate URL -> local path once, into
``data["markdown"]``, so :meth:`canonical_remote`/:meth:`render_local` (which get no
``ctx`` at all — the :class:`~grison.engine.adapter.Adapter` protocol doesn't pass one
to those) can just use it verbatim, already in the SAME vocabulary
:meth:`canonical_local` compares against (the local file's own, as-authored body);
:meth:`create`/:meth:`update`/:meth:`restore` translate local path -> URL right before
sending. See :mod:`grison.adapters.bs_images` for the ``images/`` folder <-> gallery
row mechanism this depends on.
"""

from __future__ import annotations

import re
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

_RECYCLE_REASON = "in the BookStack recycle bin, recoverable there"

# --- gallery image line <-> absolute URL translation (D9/BRIEF task C) --------------

_LOCAL_IMAGE_LINE_RE = re.compile(
    r'(!\[[^\]]*\]\()(images/|\.\./images/)([^)\s"]+)((?:\s+"[^"]*")?\))'
)
_URL_IMAGE_LINE_RE = re.compile(r'(!\[[^\]]*\]\()(https?://[^)\s"]+)((?:\s+"[^"]*")?\))')


def _local_body_to_remote(body: str, gallery_by_name: dict[str, str]) -> str:
    """``images/x.png``/``../images/x.png`` -> the book's absolute gallery URL for
    ``x.png`` (a name with no matching gallery row is left exactly as written — an
    unresolved reference is a validator failure, REF-002, never guessed here)."""

    def _sub(m: re.Match[str]) -> str:
        prefix, _rel, name, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        url = gallery_by_name.get(name)
        return f"{prefix}{url}{suffix}" if url else m.group(0)

    return _LOCAL_IMAGE_LINE_RE.sub(_sub, body)


def _remote_body_to_local(body: str, gallery_by_url: dict[str, str], *, in_chapter: bool) -> str:
    """The absolute gallery URL BookStack stores -> the ONE correct local spelling
    for this page's location (REF-007: ``images/`` at the book root, ``../images/``
    inside a chapter) — the PULL-side counterpart of :func:`_local_body_to_remote`."""
    rel_prefix = "../images/" if in_chapter else "images/"

    def _sub(m: re.Match[str]) -> str:
        prefix, url, suffix = m.group(1), m.group(2), m.group(3)
        name = gallery_by_url.get(url)
        return f"{prefix}{rel_prefix}{name}{suffix}" if name else m.group(0)

    return _URL_IMAGE_LINE_RE.sub(_sub, body)


def _matched_names_and_urls(body: str) -> tuple[list[str], list[str]]:
    """Every gallery image reference in ``body``, split by spelling: local names
    (``images/x.png``/``../images/x.png``, in document order) and raw absolute
    URLs (in document order) — the two vocabularies :func:`_local_body_to_remote`/
    :func:`_remote_body_to_local` translate between."""
    names = [m.group(3) for m in _LOCAL_IMAGE_LINE_RE.finditer(body)]
    urls = [m.group(2) for m in _URL_IMAGE_LINE_RE.finditer(body)]
    return names, urls


def _gallery_by_name_for_book(ctx: BSContext, book_id: int) -> dict[str, str]:
    """filename -> absolute gallery URL, for every gallery image belonging to
    ``book_id`` (:func:`grison.adapters.bs_images.page_ids_in_book`) — cached on
    ``ctx.gallery_cache`` for the rest of this sync run. ``fetch_remote``/
    ``refetch``/``create``/``update`` all resolve a book's gallery through this ONE
    call site, so a book with several pages pays for the page-list + gallery-list
    fetch once per run, not once per page pushed."""
    cached = ctx.gallery_cache.get(book_id)
    if cached is not None:
        return cached
    from grison.adapters.bs_images import page_ids_in_book

    page_ids = page_ids_in_book(ctx.client, ctx, book_id)
    out: dict[str, str] = {}
    for row in ctx.client.fetch_gallery_images():
        if row.get("uploaded_to") not in page_ids:
            continue
        name = PurePosixPath(row.get("name") or "").name
        url = row.get("url") or ""
        if name and url:
            out[name] = url
    ctx.gallery_cache[book_id] = out
    return out


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


@dataclass
class BsPageAdapter:
    """``methodology/library/**/*.md`` <-> BookStack pages.

    ``canonical_local``/``canonical_remote`` (the ``Adapter`` protocol's two
    ctx-less methods) fold every gallery image reference's CURRENT remote id into
    the hash (:meth:`_fold_image_ids`) — the SAME "local file identity" fix
    :mod:`grison.adapters.gw_report`/:mod:`grison.adapters.gw_findings` apply via
    :func:`grison.engine.filesets.canonical_prose`'s ``embed_ids``, adapted to D9's
    own vocabulary (a stored absolute gallery URL, not an html node id). Before
    this fix, a reupload of an image a page references (D9: bytes changing under
    an unchanged filename creates a NEW gallery row, the old one deleted — see
    :mod:`grison.adapters.bs_images`) left the page's OLD, now-orphaned URL
    unresolved by :meth:`_localize_gallery_urls` (no CURRENT row has that URL any
    more): the page's canonical remote form kept the literal stale URL text, which
    (since the local file never changed) still hashed equal to ``base`` — so the
    page classified PULL and got silently overwritten with the raw URL instead of
    ever surfacing the new one. Folding each reference's id (resolved fresh, by
    filename for a local-spelled reference or directly by URL for one that's still
    raw) makes the LOCAL side's fold change the moment the id underneath its
    filename changes, so local no longer equals base and the page is never
    overwritten (see ``tests/e2e/test_wiki_images_e2e.py`` for the resulting
    COLLISION + ``--force-local`` resolution — the same "COLLISION, not an
    automatic same-run push" shape :mod:`grison.adapters.gw_report`'s own reupload
    fix reaches, for the identical reason: nothing marks the referencing document's
    OWN stored text/HTML stale until it is actually re-pushed).

    Resolving a reference needs the live gallery (``ctx`` — the ``Adapter``
    protocol doesn't hand ``canonical_local``/``canonical_remote`` one at all), so
    ``self._ctx``/``self._url_to_id_cache`` are captured as a side effect of
    ``fetch_remote``/``refetch``/``create``/``update`` — the only methods that DO
    receive ``ctx`` — mirroring :class:`grison.adapters.gw_report.
    NarrativeSectionAdapter`'s identical ``self._index`` caching (see that class's
    own docstring for why this is safe: :mod:`grison.engine.apply`'s loop order
    guarantees ``fetch_remote`` runs before any ``canonical_local``/
    ``canonical_remote`` call on the SAME adapter instance within one sync). The
    one caller with no ctx-bearing call at all (:func:`grison.engine.offline_status.
    compute_offline_status`) degrades to folding no ids, never a crash."""

    kind = "bs.page"
    mode: AdapterMode = "read-write"
    _ctx: BSContext | None = field(default=None, init=False, repr=False, compare=False)
    _url_to_id_cache: dict[str, int] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def _url_to_id(self, ctx: BSContext) -> dict[str, int]:
        """Every CURRENT gallery image's absolute URL -> id, org-wide (BookStack's
        URL is unique per row regardless of book) — cached on this adapter
        instance for the whole sync run, the same one ``fetch_gallery_images()``
        call :func:`_gallery_by_name_for_book` already pays for per book, just
        keyed the other way."""
        if self._url_to_id_cache is None:
            self._url_to_id_cache = {
                row["url"]: row["id"] for row in ctx.client.fetch_gallery_images()
                if row.get("url")
            }
        return self._url_to_id_cache

    def _fold_image_ids(
        self, ctx: BSContext | None, body: str, *, book_id: int | None,
    ) -> list[int | None]:
        """Every gallery image reference in ``body`` (either spelling), resolved to
        its CURRENT remote id, in document order — ``None`` where ``ctx``/
        ``book_id`` aren't available (offline) or a reference can't currently be
        resolved at all (a genuinely orphaned/broken reference), never fatal."""
        if ctx is None or book_id is None:
            return []
        names, urls = _matched_names_and_urls(body)
        if not names and not urls:
            return []
        url_to_id = self._url_to_id(ctx)
        gallery_by_name = _gallery_by_name_for_book(ctx, book_id)
        ids: list[int | None] = [url_to_id.get(gallery_by_name.get(n, "")) for n in names]
        ids.extend(url_to_id.get(u) for u in urls)
        return ids

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
        self._ctx = ctx
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
            data = _normalize(detail, books_by_id, chapters_by_id)
            self._localize_gallery_urls(ctx, data)
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
                id=rid, data={"_recycled": True, "id": rid, "name": deletable.get("name", "")},
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
        self._localize_gallery_urls(ctx, data)
        return RemoteRecord(id=id, data=data,
                            witness={"updated_at": detail.get("updated_at"),
                                    "revision_count": detail.get("revision_count")})

    def _localize_gallery_urls(self, ctx: BSContext, data: dict[str, Any]) -> None:
        """Mutates ``data["markdown"]`` in place: absolute gallery URL -> the one
        correct local spelling for this page's location (D9/BRIEF task C — see
        module docstring). Cheap no-op when the body has no gallery image line at
        all (the regex never matches). ``_gallery_by_name_for_book`` does its own
        per-run caching on ``ctx.gallery_cache`` now, so every caller just asks it
        directly instead of threading a call-local cache dict through."""
        book_id = data.get("book_id")
        if book_id is None or "images/" not in (data.get("markdown") or ""):
            return
        gallery_by_name = _gallery_by_name_for_book(ctx, book_id)
        gallery_by_url = {url: name for name, url in gallery_by_name.items()}
        data["markdown"] = _remote_body_to_local(
            data["markdown"], gallery_by_url, in_chapter=data.get("chapter_id") is not None,
        )

    def canonical_local(self, doc: PageDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        book_id = self._ctx.books_by_slug.get(doc.book, {}).get("id") if self._ctx else None
        return {"title": doc.title, "priority": doc.priority, "tags": doc.tags,
                "body": doc.body, "book": doc.book, "chapter": doc.chapter,
                "image_ids": self._fold_image_ids(self._ctx, doc.body, book_id=book_id)}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        if data.get("_recycled") or data.get("_skipped"):
            # never hashed for a real comparison: _recycled is always vetoed to SKIP
            # before a write; _skipped supplies cached_hash instead of calling this.
            return {"_unavailable": True}
        body = (data.get("markdown") or "").strip()
        return {"title": data["name"], "priority": data.get("priority"),
                "tags": _tags_to_local(data.get("tags") or []),
                "body": body, "book": data["book_slug"], "chapter": data.get("chapter_slug"),
                "image_ids": self._fold_image_ids(self._ctx, body, book_id=data.get("book_id"))}

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

    def _remote_body_for_push(self, ctx: BSContext, doc: PageDoc, book_id: int) -> str:
        if "images/" not in doc.body:
            return doc.body
        return _local_body_to_remote(doc.body, _gallery_by_name_for_book(ctx, book_id))

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
            name=doc.title, markdown=self._remote_body_for_push(ctx, doc, real_book_id),
            book_id=book_id, chapter_id=chapter_id,
            tags=_tags_to_remote(doc.tags), priority=doc.priority,
        )
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        self._localize_gallery_urls(ctx, data)
        return RemoteRecord(id=rec["id"], data=data,
                            witness={"updated_at": rec.get("updated_at"),
                                    "revision_count": rec.get("revision_count")})

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
            id, markdown=self._remote_body_for_push(ctx, doc, real_book_id), name=doc.title,
            book_id=book_id, chapter_id=chapter_id,
            priority=doc.priority, tags=_tags_to_remote(doc.tags),
        ) or ctx.client.fetch_page(id)
        data = _normalize(rec, ctx.books_by_id, ctx.chapters_by_id)
        self._localize_gallery_urls(ctx, data)
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
        if book_id is not None and "images/" in markdown:
            # the preimage's markdown was captured already LOCALIZED (fetch_remote/
            # refetch/create/update all localize `data["markdown"]` in place — see
            # module docstring) — translate back to the URL form BookStack itself
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
