"""Tests for the BookStack REST API client.

All requests go through ``httpx.MockTransport`` — no live BookStack calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from grison.remote.bookstack import BookStackClient, BookStackError
from grison.remote.creds import Creds

_CREDS = Creds(
    bs_url="https://wiki.example",
    bs_token_id="tid",
    bs_token_secret="tsec",
    cf_client_id="cid",
    cf_client_secret="csec",
)

_BOOK_ROWS = [
    {"id": 1, "name": "Methodology", "slug": "methodology"},
    {"id": 2, "name": "Findings", "slug": "findings"},
]

_PAGE_LIST_ROWS = [
    {
        "id": 10,
        "book_id": 1,
        "book_slug": "methodology",
        "slug": "recon",
        "name": "Recon",
        "editor": "markdown",
        "updated_at": "2026-07-01T00:00:00.000000Z",
        "revision_count": 3,
    },
    {
        "id": 11,
        "book_id": 2,
        "book_slug": "findings",
        "slug": "weak-tls",
        "name": "Weak TLS",
        "editor": "markdown",
        "updated_at": "2026-07-02T00:00:00.000000Z",
        "revision_count": 1,
    },
]

_PAGE_DETAIL = {
    "id": 10,
    "book_id": 1,
    "book_slug": "methodology",
    "slug": "recon",
    "name": "Recon",
    "editor": "markdown",
    "updated_at": "2026-07-01T00:00:00.000000Z",
    "revision_count": 3,
    "markdown": "# Recon\n\nnmap etc.",
    "html": "<h1>Recon</h1>",
    "tags": [],
}

_CHAPTER_ROWS = [
    {"id": 4, "book_id": 1, "slug": "web-app", "name": "Web App", "priority": 1},
]

_SHELF_ROWS = [
    {"id": 7, "slug": "pentest-ops", "name": "Pentest Ops"},
]


def _make_transport(captured: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/api/books":
            assert request.url.params.get("count") == "500"  # BookStack's per-request cap
            return httpx.Response(200, json={"data": _BOOK_ROWS})
        if method == "GET" and path == "/api/chapters":
            assert request.url.params.get("count") == "500"  # BookStack's per-request cap
            return httpx.Response(200, json={"data": _CHAPTER_ROWS})
        if method == "GET" and path == "/api/shelves":
            assert request.url.params.get("count") == "500"  # BookStack's per-request cap
            return httpx.Response(200, json={"data": _SHELF_ROWS})
        if method == "GET" and path == "/api/shelves/7":
            return httpx.Response(
                200, json={**_SHELF_ROWS[0], "books": [{"id": 1, "slug": "methodology"}]}
            )
        if method == "GET" and path == "/api/pages":
            assert request.url.params.get("count") == "500"  # BookStack's per-request cap
            return httpx.Response(200, json={"data": _PAGE_LIST_ROWS})
        if method == "GET" and path == "/api/pages/10":
            return httpx.Response(200, json=_PAGE_DETAIL)
        if method == "PUT" and path == "/api/pages/10":
            return httpx.Response(200, json=_PAGE_DETAIL)
        if method == "POST" and path == "/api/pages":
            return httpx.Response(200, json={"id": 99})
        if method == "DELETE" and path == "/api/pages/99":
            return httpx.Response(204)
        if method == "GET" and path == "/api/pages/404":
            return httpx.Response(404, text="not found")
        if method == "GET" and path == "/api/books/1":
            return httpx.Response(200, json={**_BOOK_ROWS[0], "description": "d"})
        if method == "POST" and path == "/api/books":
            return httpx.Response(200, json={"id": 55, "name": "New Book", "slug": "new-book"})
        if method == "DELETE" and path == "/api/books/1":
            return httpx.Response(204)
        if method == "GET" and path == "/api/chapters/4":
            return httpx.Response(200, json={**_CHAPTER_ROWS[0], "description": "d"})
        if method == "POST" and path == "/api/chapters":
            return httpx.Response(
                200, json={"id": 66, "book_id": 1, "name": "New Chapter", "slug": "new-chapter"}
            )
        if method == "DELETE" and path == "/api/chapters/4":
            return httpx.Response(204)
        if method == "GET" and path == "/api/recycle-bin":
            return httpx.Response(200, json={"data": [{"id": 1, "deletable_type": "page",
                                                        "deletable_id": 10}]})

        raise AssertionError(f"unexpected request: {method} {path}")

    return httpx.MockTransport(handler)


def test_fetch_books_parses_data() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_books()
    assert rows == _BOOK_ROWS


def test_fetch_pages_parses_data() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_pages()
    assert rows == _PAGE_LIST_ROWS


def test_requests_carry_auth_and_cf_headers() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        client.fetch_books()
        client.fetch_pages()
    assert captured  # sanity: requests were actually made
    for req in captured:
        assert req.headers["Authorization"] == "Token tid:tsec"
        assert req.headers["CF-Access-Client-Id"] == "cid"
        assert req.headers["CF-Access-Client-Secret"] == "csec"


def test_fetch_page_returns_detail_with_markdown() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        page = client.fetch_page(10)
    assert page == _PAGE_DETAIL
    assert page["markdown"] == "# Recon\n\nnmap etc."


def test_update_page_sends_put_with_markdown_body() -> None:
    """update_page returns the response body — BookStack's PUT returns the updated
    page object, and methodology sync's push path restamps updated_at/revision_count
    from it without a separate GET."""
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        result = client.update_page(10, markdown="new content")
    assert result == _PAGE_DETAIL
    put_requests = [r for r in captured if r.method == "PUT"]
    assert len(put_requests) == 1
    req = put_requests[0]
    assert req.url.path == "/api/pages/10"
    assert json.loads(req.content) == {"markdown": "new content"}


def test_update_page_parent_move_params() -> None:
    """chapter_id and book_id are parent moves — sent only when given, chapter wins."""
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        client.update_page(10, markdown="x", chapter_id=4, priority=2)
        client.update_page(10, markdown="x", book_id=1)
        client.update_page(10, markdown="x", book_id=1, chapter_id=4)
    bodies = [json.loads(r.content) for r in captured if r.method == "PUT"]
    assert bodies[0] == {"markdown": "x", "chapter_id": 4, "priority": 2}
    assert bodies[1] == {"markdown": "x", "book_id": 1}
    assert bodies[2] == {"markdown": "x", "chapter_id": 4}  # never both parents at once


def test_fetch_chapters_and_shelves() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        assert client.fetch_chapters() == _CHAPTER_ROWS
        assert client.fetch_shelves() == _SHELF_ROWS
        assert client.fetch_shelf(7)["books"] == [{"id": 1, "slug": "methodology"}]


def test_create_page_posts_and_returns_record() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        rec = client.create_page(book_id=1, name="New Page", markdown="body")
    assert rec["id"] == 99
    post_requests = [r for r in captured if r.method == "POST"]
    assert len(post_requests) == 1
    req = post_requests[0]
    assert req.url.path == "/api/pages"
    assert json.loads(req.content) == {"name": "New Page", "markdown": "body", "book_id": 1}


def test_create_page_in_chapter_sends_chapter_id_only() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        rec = client.create_page(book_id=1, chapter_id=4, name="New Page", markdown="body")
    assert rec["id"] == 99
    body = json.loads([r for r in captured if r.method == "POST"][0].content)
    assert body == {"name": "New Page", "markdown": "body", "chapter_id": 4}


def test_delete_page_sends_delete_and_tolerates_empty_body() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        result = client.delete_page(99)
    assert result is None
    delete_requests = [r for r in captured if r.method == "DELETE"]
    assert len(delete_requests) == 1
    assert delete_requests[0].url.path == "/api/pages/99"


def test_list_endpoints_paginate_past_the_count_cap() -> None:
    """A wiki with more rows than one response's `count` must not silently truncate."""
    rows = [{"id": i} for i in range(2500)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/pages"
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200, json={"data": rows[offset : offset + 1000], "total": len(rows)}
        )

    with BookStackClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        pages = client.fetch_pages()
    assert len(pages) == 2500
    assert pages[-1]["id"] == 2499


def test_non_2xx_raises_bookstack_error() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        with pytest.raises(BookStackError, match="404"):
            client.fetch_page(404)


def test_fetch_book_returns_detail() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        book = client.fetch_book(1)
    assert book["description"] == "d"


def test_create_book_posts_name_and_description() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        rec = client.create_book(name="New Book", description="desc")
    assert rec["id"] == 55
    body = json.loads([r for r in captured if r.method == "POST" and r.url.path == "/api/books"
                       ][0].content)
    assert body == {"name": "New Book", "description": "desc"}


def test_create_book_omits_empty_description() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        client.create_book(name="New Book")
    body = json.loads([r for r in captured if r.method == "POST" and r.url.path == "/api/books"
                       ][0].content)
    assert body == {"name": "New Book"}


def test_delete_book_sends_delete() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        assert client.delete_book(1) is None


def test_fetch_chapter_returns_detail() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        chapter = client.fetch_chapter(4)
    assert chapter["description"] == "d"


def test_create_chapter_posts_book_id_and_name() -> None:
    captured: list[httpx.Request] = []
    with BookStackClient(_CREDS, transport=_make_transport(captured)) as client:
        rec = client.create_chapter(book_id=1, name="New Chapter")
    assert rec["id"] == 66
    body = json.loads(
        [r for r in captured if r.method == "POST" and r.url.path == "/api/chapters"][0].content
    )
    assert body == {"book_id": 1, "name": "New Chapter"}


def test_delete_chapter_sends_delete() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        assert client.delete_chapter(4) is None


def test_fetch_recycle_bin_paginates_like_other_lists() -> None:
    with BookStackClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_recycle_bin()
    assert rows == [{"id": 1, "deletable_type": "page", "deletable_id": 10}]


