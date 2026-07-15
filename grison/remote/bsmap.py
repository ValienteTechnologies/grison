"""BookStack page ⇄ local methodology markdown document, and its content hash.

Methodology pages are markdown-native since the 2026-07-14 migration, so — unlike
Ghostwriter findings — there is no HTML converter: grison mirrors the page's
``markdown`` field verbatim and PUTs it straight back. The document is YAML
frontmatter (grison + BookStack ids + the merge base) followed by the page body.
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
    synced_hash: str | None = None
    synced_at: str | None = None


def bs_content_hash(page: MethPage) -> str:
    """Merge base over the syncable surface: title + book + markdown body."""
    payload = json.dumps(
        {"title": page.title, "book": page.book, "body": page.body},
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def page_from_record(rec: dict, *, book_slug: str) -> MethPage:
    """Build a MethPage from a BookStack page detail record."""
    return MethPage(
        page_id=rec["id"],
        book_id=rec["book_id"],
        book=book_slug,
        title=rec["name"],
        body=(rec.get("markdown") or "").strip(),
    )


def page_to_markdown(page: MethPage) -> str:
    """Serialize a MethPage to its local methodology document."""
    fm: dict = {
        "grison": {
            "kind": "methodology",
            "bs": {"page_id": page.page_id, "book_id": page.book_id},
        },
        "title": page.title,
        "book": page.book,
    }
    if page.page_id is None:
        fm["grison"]["bs"].pop("page_id")
    if page.book_id is None:
        fm["grison"]["bs"].pop("book_id")
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
        synced_hash=synced.get("hash"),
        synced_at=at,
    )


def stamp(page: MethPage, *, now: datetime) -> MethPage:
    """Set the merge base (hash + time) to the page's current content."""
    page.synced_hash = bs_content_hash(page)
    page.synced_at = now.isoformat()
    return page
