"""BookStack page ⇄ local methodology markdown document, and its content hash.

Methodology pages are markdown-native since the 2026-07-14 migration, so — unlike
Ghostwriter findings — there is no HTML converter: grison mirrors the page's
``markdown`` field verbatim and PUTs it straight back. The document is YAML
frontmatter (grison + BookStack ids + the merge base) followed by the page body.

Structure is part of the mirror: a page's chapter (BookStack's book > chapter > page
nesting) and its sort order (``priority``) live in the frontmatter and in the content
hash. Both hash keys are emitted only when set, so documents written before chapter
awareness keep their merge base valid — the first chapter-aware sync sees them as
remote-changed and migrates them via ordinary pulls.

``chapter_id`` is tri-state: ``None`` means the document predates chapter awareness
(its book-root location says nothing about chapters), ``0`` means deliberately at the
book root, ``>0`` names the chapter. Push uses this to avoid ejecting a remotely
chaptered page just because a legacy file sits at the book root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

import yaml


@dataclass
class MethPage:
    page_id: int | None
    book_id: int | None
    book: str  # book slug (also the directory)
    title: str
    body: str  # the page markdown
    chapter: str | None = None  # chapter slug (also the subdirectory); None = book root
    chapter_id: int | None = None  # None = pre-chapter-era doc; 0 = book root; >0 = chapter
    priority: int | None = None  # BookStack sort order within the parent
    tags: list[dict] | None = None  # [{"name","value"}]; None = pre-tag-era doc
    synced_hash: str | None = None
    synced_at: str | None = None


def bs_content_hash(page: MethPage) -> str:
    """Merge base over the syncable surface: title + book + chapter + priority + body.

    ``chapter``/``priority`` join the payload only when set — pre-chapter-era hashes
    stay valid, so upgrading grison migrates via pulls instead of collisions."""
    payload_dict: dict = {"title": page.title, "book": page.book, "body": page.body}
    if page.chapter:
        payload_dict["chapter"] = page.chapter
    if page.priority is not None:
        payload_dict["priority"] = page.priority
    if page.tags:
        payload_dict["tags"] = page.tags
    payload = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


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
    )


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
    """Serialize a MethPage to its local methodology document."""
    fm: dict = {
        "grison": {
            "kind": "methodology",
            "bs": {
                "page_id": page.page_id,
                "book_id": page.book_id,
                "chapter_id": page.chapter_id,
            },
        },
        "title": page.title,
        "book": page.book,
    }
    if page.page_id is None:
        fm["grison"]["bs"].pop("page_id")
    if page.book_id is None:
        fm["grison"]["bs"].pop("book_id")
    if page.chapter_id is None:
        fm["grison"]["bs"].pop("chapter_id")  # chapter_id: 0 stays — it marks chapter awareness
    if page.chapter:
        fm["chapter"] = page.chapter
    if page.priority is not None:
        fm["priority"] = page.priority
    if page.tags:
        # value-less tags serialize as bare strings — the common case reads cleanly
        fm["tags"] = [t["name"] if not t["value"] else dict(t) for t in page.tags]
    if page.synced_hash:
        fm["grison"]["synced"] = {"hash": page.synced_hash, "at": page.synced_at}
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    body = page.body.strip()
    return f"---\n{fm_yaml}\n---\n\n{body}\n" if body else f"---\n{fm_yaml}\n---\n"


def markdown_to_page(text: str) -> MethPage:
    """Parse a local methodology document back into a MethPage."""
    if not text.startswith("---"):
        raise ValueError("methodology document has no frontmatter")
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            body = "\n".join(lines[i + 1 :]).strip()
            break
    else:
        raise ValueError("unterminated frontmatter")
    grison = fm.get("grison", {})
    bs = grison.get("bs", {})
    synced = grison.get("synced") or {}
    at = synced.get("at")
    if isinstance(at, datetime):
        at = at.isoformat()
    return MethPage(
        page_id=bs.get("page_id"),
        book_id=bs.get("book_id"),
        book=fm.get("book", ""),
        title=fm.get("title", ""),
        body=body,
        chapter=fm.get("chapter"),
        chapter_id=bs.get("chapter_id"),
        priority=fm.get("priority"),
        tags=_norm_tags(fm["tags"]) if fm.get("tags") is not None else None,
        synced_hash=synced.get("hash"),
        synced_at=at,
    )


def stamp(page: MethPage, *, now: datetime) -> MethPage:
    """Set the merge base (hash + time) to the page's current content."""
    page.synced_hash = bs_content_hash(page)
    page.synced_at = now.isoformat()
    return page
