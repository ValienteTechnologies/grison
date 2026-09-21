"""Remote-row normalisation, BookStack tag <-> v2 frontmatter conversion, and
:class:`PageDoc` (the local-side shape) — see :mod:`grison.adapters.bs_pages`'s own
docstring for the shared shape."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    detail: dict[str, Any],
    books_by_id: dict[int, dict[str, Any]],
    chapters_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    book_id = detail["book_id"]
    chapter_id = detail.get("chapter_id") or 0
    return {
        "id": detail["id"],
        "name": detail["name"],
        "slug": detail.get("slug") or "",
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
