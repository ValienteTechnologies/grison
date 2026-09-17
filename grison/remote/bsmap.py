"""BookStack page ⇄ local methodology markdown document, and its content hash.

Methodology pages are markdown-native, so — unlike Ghostwriter findings — there is
no converter: grison mirrors the page's ``markdown`` field verbatim and PUTs it
straight back. The document is YAML frontmatter (grison + BookStack ids + the merge
base) followed by the page body.

Structure is part of the mirror: a page's chapter (BookStack's book > chapter > page
nesting), its sort order (``priority``) and its tags live in the frontmatter and in
the content hash, so structure changes reconcile 3-way exactly like body edits.
``chapter_id: 0`` means the page sits at the book root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from grison import hashing
from grison.markdown import frontmatter as fm


@dataclass
class MethPage:
    page_id: int | None
    book_id: int | None
    book: str  # book slug (also the directory)
    title: str
    body: str  # the page markdown
    chapter: str | None = None  # chapter slug (also the subdirectory); None = book root
    chapter_id: int = 0  # 0 = book root
    priority: int | None = None  # BookStack sort order within the parent
    tags: list[dict] = field(default_factory=list)  # [{"name","value"}]
    synced_hash: str | None = None
    synced_at: str | None = None
    # BookStack's own change markers off the page row (list AND detail expose both) —
    # bump on every update_page call, content or not, never fail to bump on a content
    # change. Not part of bs_content_hash: they gate the sync's skip-detail-fetch fast
    # path (grison/remote/methodology.py), they don't describe content themselves.
    remote_updated_at: str | None = None
    remote_revision_count: int | None = None


def bs_content_hash(page: MethPage) -> str:
    """Merge base over the full syncable surface: title + location + order + tags + body."""
    return hashing.digest_legacy(
        {
            "title": page.title,
            "book": page.book,
            "chapter": page.chapter or "",
            "priority": page.priority,
            "tags": page.tags,
            "body": page.body,
        }
    )


def page_from_record(rec: dict, *, book_slug: str, chapter_slug: str | None = None) -> MethPage:
    """Build a MethPage from a BookStack page detail record."""
    return MethPage(
        page_id=rec["id"],
        book_id=rec["book_id"],
        book=book_slug,
        title=rec["name"],
        body=(rec.get("markdown") or "").strip(),
        chapter=chapter_slug,
        chapter_id=rec.get("chapter_id") or 0,
        priority=rec.get("priority"),
        tags=_norm_tags(rec.get("tags") or []),
        remote_updated_at=rec.get("updated_at"),
        remote_revision_count=rec.get("revision_count"),
    )


def is_markdown_native(rec: dict) -> bool:
    """True when a BookStack page record is safe for grison's markdown mirror.

    BookStack pages carry content in either ``markdown`` (``editor="markdown"``) or
    ``html``/``raw_html`` (wysiwyg flavors); grison only ever reads/writes the former.
    False when ``editor`` is a non-markdown flavor, or — defensively, for installs
    where ``editor`` itself might be stale or absent — when ``markdown`` is empty
    while real rendered content exists (``raw_html``/``html`` non-empty), which is
    exactly the wysiwyg-authored-page shape that must never be silently mirrored as
    an empty body nor overwritten via the markdown PUT param."""
    editor = rec.get("editor")
    if editor and editor != "markdown":
        return False
    markdown = (rec.get("markdown") or "").strip()
    html = (rec.get("raw_html") or rec.get("html") or "").strip()
    return bool(markdown) or not html


def _norm_tags(raw: list) -> list[dict]:
    """Normalize tags to [{"name","value"}] — the shape hashed and PUT back. Accepts
    BookStack records (extra keys dropped) and frontmatter shorthand (a bare string
    is a value-less tag)."""
    tags: list[dict] = []
    for t in raw:
        if isinstance(t, dict):
            tags.append({"name": str(t.get("name") or ""), "value": str(t.get("value") or "")})
        else:
            tags.append({"name": str(t), "value": ""})
    return tags


def page_to_markdown(page: MethPage) -> str:
    """Serialize a MethPage to its local methodology document — content + identity
    only (title/book/chapter/priority/tags/body, and ``grison.bs.page_id``). The merge
    base, BookStack's change markers, and the book/chapter id witnesses are volatile/
    derived and live in the state store (grison/state.py), never in the tracked file."""
    meta: dict = {
        "grison": {
            "kind": "methodology",
            "bs": {"page_id": page.page_id},
        },
        "title": page.title,
        "book": page.book,
    }
    if page.page_id is None:
        meta["grison"]["bs"].pop("page_id")
    if page.chapter:
        meta["chapter"] = page.chapter
    if page.priority is not None:
        meta["priority"] = page.priority
    if page.tags:
        # value-less tags serialize as bare strings — the common case reads cleanly
        meta["tags"] = [t["name"] if not t["value"] else dict(t) for t in page.tags]
    return fm.dump(meta, page.body)


def markdown_to_page(text: str) -> MethPage:
    """Parse a local methodology document back into a MethPage — content + identity
    only. The merge base, remote change markers, and book/chapter id witnesses are
    left at their defaults; the caller hydrates them from the state store right after
    this returns (grison/remote/methodology.py's ``_hydrate_page``)."""
    meta, body = fm.split(text)
    body = body.strip()
    grison = meta.get("grison", {})
    bs = grison.get("bs", {})
    return MethPage(
        page_id=bs.get("page_id"),
        book_id=None,
        book=meta.get("book", ""),
        title=meta.get("title", ""),
        body=body,
        chapter=meta.get("chapter"),
        priority=meta.get("priority"),
        tags=_norm_tags(meta.get("tags") or []),
    )


def stamp(
    page: MethPage,
    *,
    now: datetime,
    remote_updated_at: str | None = None,
    remote_revision_count: int | None = None,
) -> MethPage:
    """Set the merge base (hash + time) to the page's current content, and record
    BookStack's own change markers off whatever response prompted this stamp — absent
    (None) when that response didn't carry them, which safely forces a detail fetch
    next sync rather than trusting a stale or missing marker."""
    page.synced_hash = bs_content_hash(page)
    page.synced_at = now.isoformat()
    page.remote_updated_at = remote_updated_at
    page.remote_revision_count = remote_revision_count
    return page
