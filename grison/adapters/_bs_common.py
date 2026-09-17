"""Shared BookStack lookups both wiki adapters need — never imported by
:mod:`grison.engine` itself (see ``tests/test_engine_no_leak.py``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from grison.engine.state import StateStore
from grison.remote.bookstack import BookStackClient

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A BookStack-compatible slug from a directory/title-shaped string — mirrors
    BookStack's own slugification closely enough that a directory named exactly this
    way round-trips (D4: directory names are stable handles, derived once on pull and
    never renamed)."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "page"


@dataclass
class BSContext:
    """Everything the wiki adapters need for one sync run: the live client plus
    books/chapters looked up both ways (fetched once, reused by both adapters and
    refreshed after :func:`grison.adapters.bs_structure.sync_structure` creates any
    new book/chapter, so page identity resolution always sees the current tree)."""

    client: BookStackClient
    state: StateStore | None = None
    books: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)

    @property
    def books_by_id(self) -> dict[int, dict[str, Any]]:
        return {b["id"]: b for b in self.books}

    @property
    def books_by_slug(self) -> dict[str, dict[str, Any]]:
        return {b["slug"]: b for b in self.books}

    @property
    def chapters_by_id(self) -> dict[int, dict[str, Any]]:
        return {c["id"]: c for c in self.chapters}

    @property
    def chapters_by_book_slug(self) -> dict[tuple[int, str], dict[str, Any]]:
        return {(c["book_id"], c["slug"]): c for c in self.chapters}

    def refresh(self) -> None:
        self.books = self.client.fetch_books()
        self.chapters = self.client.fetch_chapters()


def build_context(client: BookStackClient, state: StateStore | None = None) -> BSContext:
    ctx = BSContext(client=client, state=state)
    ctx.refresh()
    return ctx
