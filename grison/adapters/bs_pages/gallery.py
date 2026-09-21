"""Gallery image line <-> absolute BookStack gallery URL translation (D9/BRIEF task
C) — see :mod:`grison.adapters.bs_pages`'s own docstring for when each direction
runs and why."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from grison.adapters._bs_common import BSContext

from .normalize import PageDoc

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


_URL_NUMERIC_ID_RE = re.compile(r"/(\d+)/[^/]+$")


def _gallery_token(url: str) -> str:
    """``url`` -> a canonicalization token that is a PURE FUNCTION of the URL
    STRING itself — never a live gallery lookup (D9: "replacing an image's
    bytes must re-push every page referencing it, automatically, in the same
    run" — see :func:`_substitute_gallery_tokens`'s docstring for why a
    live lookup is exactly the bug this avoids). A numeric id segment
    immediately before the filename (this fake's/some real installs' URL
    shape) is used when present — it's the more precise identity and, being
    read directly off the URL text, is exactly as "literal" as the URL
    itself; otherwise the URL is used verbatim (real BookStack's usual
    ``.../gallery/YYYY-MM/name.ext`` shape has no such segment at all)."""
    m = _URL_NUMERIC_ID_RE.search(url)
    return m.group(1) if m else url


def _substitute_gallery_tokens(ctx: BSContext | None, body: str, *, book_id: int | None) -> str:
    """Replace every gallery image reference's PATH/URL portion (either
    spelling) with its :func:`_gallery_token` — alt text and title are kept
    exactly as authored (unlike a Ghostwriter evidence embed's caption, a
    BookStack page's alt text has no separate remote row to belong to
    instead — D9: "BookStack's gallery has no caption column at all" — so it
    stays ordinary document content, part of what a real edit to it should
    change). Both spellings of a reference to the SAME image reduce to the
    SAME text: replacing the PATH itself, not just adding a parallel fold
    list, is what actually makes :meth:`~grison.adapters.bs_pages.adapter.
    BsPageAdapter.canonical_local`'s result comparable to :meth:`~grison.
    adapters.bs_pages.adapter.BsPageAdapter.canonical_remote`'s — the raw
    ``body``/``markdown`` text alone differs between an authored
    ``images/x.png`` and BookStack's stored absolute URL even when they name
    the SAME row, so a fold list alongside the UNCHANGED raw text is not
    enough (the "body" field would still differ) — see :meth:`~grison.
    adapters.bs_pages.adapter.BsPageAdapter.canonical_remote`'s docstring for
    the full D9 reasoning this exists to satisfy. ``ctx``/``book_id``
    unavailable (offline) degrades to the raw ``body``, unchanged, same as
    before this fix."""
    if ctx is None or book_id is None:
        return body
    gallery_by_name = _gallery_by_name_for_book(ctx, book_id)

    def _local_sub(m: re.Match[str]) -> str:
        name = m.group(3)
        url = gallery_by_name.get(name)
        token = _gallery_token(url) if url is not None else "unresolved"
        return f"{m.group(1)}{token}{m.group(4)}"

    def _url_sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{_gallery_token(m.group(2))}{m.group(3)}"

    body = _LOCAL_IMAGE_LINE_RE.sub(_local_sub, body)
    return _URL_IMAGE_LINE_RE.sub(_url_sub, body)


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


def _localize_gallery_urls(ctx: BSContext, data: dict[str, Any]) -> None:
    """Mutates ``data["markdown"]`` in place: absolute gallery URL -> the one
    correct local spelling for this page's location (D9/BRIEF task C — see
    package docstring). Cheap no-op when the body has no gallery image line at
    all (the regex never matches). ``_gallery_by_name_for_book`` does its own
    per-run caching on ``ctx.gallery_cache`` now, so every caller just asks it
    directly instead of threading a call-local cache dict through. Called by
    :class:`~grison.adapters.bs_pages.adapter.BsPageAdapter`'s ``fetch_remote``/
    ``refetch``/``create``/``update``."""
    book_id = data.get("book_id")
    if book_id is None or "images/" not in (data.get("markdown") or ""):
        return
    gallery_by_name = _gallery_by_name_for_book(ctx, book_id)
    gallery_by_url = {url: name for name, url in gallery_by_name.items()}
    data["markdown"] = _remote_body_to_local(
        data["markdown"],
        gallery_by_url,
        in_chapter=data.get("chapter_id") is not None,
    )


def _remote_body_for_push(ctx: BSContext, doc: PageDoc, book_id: int) -> str:
    """A page's about-to-be-pushed local body, gallery-translated for BookStack
    (the PUSH-side counterpart of :func:`_localize_gallery_urls`) — called by
    :class:`~grison.adapters.bs_pages.adapter.BsPageAdapter`'s ``create``/
    ``update``."""
    if "images/" not in doc.body:
        return doc.body
    return _local_body_to_remote(doc.body, _gallery_by_name_for_book(ctx, book_id))
