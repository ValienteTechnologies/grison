"""The workspace's own book/chapter/page slug namespace, for WIKI-007's offline
internal-link resolution — there is no network to ask BookStack, so this is derived
entirely from directory names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it.tree import SyntaxTreeNode

from grison.remote.creds import load as load_creds
from grison.validator import registry
from grison.validator.registry import Failure, fail


@dataclass(frozen=True)
class WikiSlugs:
    """Every book/chapter/page slug this workspace's own directory names imply, for
    WIKI-007's internal-link resolution. Computed once per :func:`validate_workspace`
    call from the tree itself (there is no network to ask BookStack)."""

    books: frozenset[str]
    chapters: dict[str, frozenset[str]]
    pages: dict[str, frozenset[str]]


def _collect_one_book_slugs(book_dir: Path) -> tuple[frozenset[str], frozenset[str]]:
    """``(chapter slugs, page slugs)`` for one book-shaped directory (a book under
    ``methodology/library``, or one engagement under ``methodology/checklists`` —
    same shape, ``[<chapter>/]*.md`` plus an optional ``images/``)."""
    cdirs = [c for c in book_dir.iterdir() if c.is_dir() and c.name != "images"]
    chapters = frozenset(c.name for c in cdirs)
    page_stems = {p.stem for p in book_dir.glob("*.md")}
    for c in cdirs:
        page_stems |= {p.stem for p in c.glob("*.md")}
    return chapters, frozenset(page_stems)


def _collect_wiki_slugs(base: Path) -> WikiSlugs:
    """Every book (direct subdirectory of ``base``, ``.shelves`` excluded) and its
    chapter/page slugs. ``base`` is ``methodology/library`` for the real wiki."""
    books: set[str] = set()
    chapters: dict[str, frozenset[str]] = {}
    pages: dict[str, frozenset[str]] = {}
    if not base.is_dir():
        return WikiSlugs(frozenset(), {}, {})
    for bdir in sorted(p for p in base.iterdir() if p.is_dir() and p.name != ".shelves"):
        books.add(bdir.name)
        chapters[bdir.name], pages[bdir.name] = _collect_one_book_slugs(bdir)
    return WikiSlugs(frozenset(books), chapters, pages)


def _merge_wiki_slugs(a: WikiSlugs, b: WikiSlugs) -> WikiSlugs:
    chapters = dict(a.chapters)
    for k, v in b.chapters.items():
        chapters[k] = chapters.get(k, frozenset()) | v
    pages = dict(a.pages)
    for k, v in b.pages.items():
        pages[k] = pages.get(k, frozenset()) | v
    return WikiSlugs(a.books | b.books, chapters, pages)


def _checklist_slugs(engagement_dir: Path, library_slugs: WikiSlugs) -> WikiSlugs:
    """A checklist engagement's own internal-link/image namespace: its own tree
    (any NEW page/chapter an agent adds while filling it in) PLUS the full
    ``methodology/library`` namespace (format choice — a checklist is a working copy
    of library content, so an inherited internal link still legitimately names the
    ORIGINAL library book it was copied from; resolving against the copy alone would
    make every such inherited link fail)."""
    chapters, pages = _collect_one_book_slugs(engagement_dir)
    own = WikiSlugs(
        frozenset({engagement_dir.name}),
        {engagement_dir.name: chapters},
        {engagement_dir.name: pages},
    )
    return _merge_wiki_slugs(library_slugs, own)


def _bs_host(root: Path) -> str | None:
    """The workspace's own BookStack host, if ``.grison/env``/env vars name one —
    used only to recognize an ABSOLUTE internal link to it (WIKI-007); never
    contacted."""
    try:
        url = load_creds(root).bs_url
    except Exception:  # noqa: BLE001 — malformed/partial creds must never break validate
        return None
    return urlsplit(url).netloc or None


_INTERNAL_LINK_RE = re.compile(r"^/books/(?P<book>[^/]+)/(?P<kind>page|chapter)/(?P<slug>[^/]+)/?$")


def _check_internal_links(
    rel: str, tree: SyntaxTreeNode, slugs: WikiSlugs, bs_host: str | None
) -> list[Failure]:
    out: list[Failure] = []
    for node in tree.walk():
        if node.type != "link":
            continue
        href = node.attrs.get("href")
        if not isinstance(href, str):
            continue
        path_part: str | None
        if href.startswith(("http://", "https://")):
            parsed = urlsplit(href)
            path_part = parsed.path if bs_host and parsed.netloc == bs_host else None
        elif href.startswith("/books/"):
            path_part = href
        else:
            path_part = None
        if path_part is None:
            continue
        m = _INTERNAL_LINK_RE.match(path_part)
        if not m:
            continue
        # lower-cased: a BookStack collision suffix is mixed-case (`notes-aBc`) while
        # the pulled file is named by the lower-case slug (WS-001, bs_pages.default_path)
        book, kind, slug = m.group("book"), m.group("kind"), m.group("slug").lower()
        target = (
            slugs.chapters.get(book, frozenset())
            if kind == "chapter"
            else slugs.pages.get(book, frozenset())
        )
        if book not in slugs.books or slug not in target:
            out.append(fail(registry.WIKI_BROKEN_INTERNAL_LINK, rel, f"{href!r} does not resolve"))
    return out
