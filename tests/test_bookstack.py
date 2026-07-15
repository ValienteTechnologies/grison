"""Tests for the BookStack REST API client.

All requests go through ``httpx.MockTransport`` — no live BookStack calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from grison.remote import snapshot as snapshot_mod
from grison.remote.bookstack import BookStackClient, BookStackError
from grison.remote.bsmap import markdown_to_page, page_to_markdown
from grison.remote.creds import Creds
from grison.remote.methodology import sync_methodology

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
            assert request.url.params.get("count") == "1000"
            return httpx.Response(200, json={"data": _BOOK_ROWS})
        if method == "GET" and path == "/api/chapters":
            assert request.url.params.get("count") == "1000"
            return httpx.Response(200, json={"data": _CHAPTER_ROWS})
        if method == "GET" and path == "/api/shelves":
            assert request.url.params.get("count") == "1000"
            return httpx.Response(200, json={"data": _SHELF_ROWS})
        if method == "GET" and path == "/api/shelves/7":
            return httpx.Response(
                200, json={**_SHELF_ROWS[0], "books": [{"id": 1, "slug": "methodology"}]}
            )
        if method == "GET" and path == "/api/pages":
            assert request.url.params.get("count") == "1000"
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


# --- methodology sync's delta gate: exercised through the real BookStackClient, so the
# HTTP call count itself is the assertion (not just the resulting classification). Unlike
# _make_transport above, PUT/POST payloads here actually mutate stored state and
# updated_at/revision_count bump on every write — mirrors the real API closely enough to
# make "did the gate skip the GET" a meaningful question. ---------------------------------


class _StatefulServer:
    """A minimal mutable BookStack double. ``handler`` is an ``httpx.MockTransport``
    callback; ``fetch_page_calls`` counts GET /api/pages/<id> — the call the delta gate
    exists to avoid."""

    def __init__(self) -> None:
        self.books = [{"id": 1, "name": "Methodology", "slug": "methodology"}]
        self.chapters: list[dict] = []
        self.shelves: list[dict] = []
        self.pages: dict[int, dict] = {}
        self.fetch_page_calls = 0
        self._clock = 0
        self._next_id = 900

    def touch(self, page: dict) -> None:
        """Bump updated_at/revision_count as BookStack does on every update_page call —
        content edit, move, tag change, or an empty PUT — never skipped on a real change."""
        self._clock += 1
        page["updated_at"] = f"2026-01-01T00:00:{self._clock:02d}.000000Z"
        page["revision_count"] = page.get("revision_count", 0) + 1

    def seed(self, pid: int, slug: str, name: str, markdown: str, **extra: object) -> None:
        rec: dict = {"id": pid, "book_id": 1, "chapter_id": 0, "slug": slug, "name": name,
                     "markdown": markdown, "editor": "markdown", "tags": []}
        rec.update(extra)
        self.touch(rec)
        self.pages[pid] = rec

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == "/api/books":
            return httpx.Response(200, json={"data": self.books, "total": len(self.books)})
        if method == "GET" and path == "/api/chapters":
            return httpx.Response(200, json={"data": self.chapters, "total": len(self.chapters)})
        if method == "GET" and path == "/api/shelves":
            return httpx.Response(200, json={"data": self.shelves, "total": len(self.shelves)})
        if method == "GET" and path == "/api/pages":
            rows = [
                {"id": p["id"], "book_id": p["book_id"], "book_slug": "methodology",
                 "chapter_id": p.get("chapter_id", 0), "slug": p["slug"], "name": p["name"],
                 "editor": p.get("editor", "markdown"), "updated_at": p["updated_at"],
                 "revision_count": p["revision_count"]}
                for p in self.pages.values()
            ]
            return httpx.Response(200, json={"data": rows, "total": len(rows)})
        if method == "GET" and path.startswith("/api/pages/"):
            self.fetch_page_calls += 1
            pid = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=dict(self.pages[pid]))
        if method == "PUT" and path.startswith("/api/pages/"):
            pid = int(path.rsplit("/", 1)[-1])
            body = json.loads(request.content)
            p = self.pages[pid]
            if "markdown" in body:
                p["markdown"] = body["markdown"]
            if "name" in body:
                p["name"] = body["name"]
            if "priority" in body:
                p["priority"] = body["priority"]
            if "tags" in body:
                p["tags"] = body["tags"]
            if "book_id" in body:  # re-parent to book root — real API semantics
                p["book_id"], p["chapter_id"] = body["book_id"], 0
            if "chapter_id" in body:
                p["chapter_id"] = body["chapter_id"]
            self.touch(p)
            return httpx.Response(200, json=dict(p))
        if method == "POST" and path == "/api/pages":
            body = json.loads(request.content)
            pid = self._next_id
            self._next_id += 1
            rec = {"id": pid, "book_id": body.get("book_id", 1),
                   "chapter_id": body.get("chapter_id") or 0, "name": body["name"],
                   "slug": body["name"].lower().replace(" ", "-"),
                   "markdown": body["markdown"], "editor": "markdown",
                   "tags": body.get("tags", [])}
            if "priority" in body:
                rec["priority"] = body["priority"]
            self.touch(rec)
            self.pages[pid] = rec
            return httpx.Response(200, json=dict(rec))
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


def _page_path(tmp_path: Path) -> Path:
    return tmp_path / "methodology" / "library" / "methodology" / "recon.md"


def test_noop_sync_skips_detail_fetch_entirely(tmp_path: Path) -> None:
    server = _StatefulServer()
    server.seed(10, "recon", "Recon", "# Recon\n\nnmap etc.")
    with BookStackClient(_CREDS, transport=httpx.MockTransport(server.handler)) as client:
        r1 = sync_methodology(tmp_path, client)
        assert server.fetch_page_calls == 1  # first sync: one fetch to seed the markers

        server.fetch_page_calls = 0
        r2 = sync_methodology(tmp_path, client)
    page = _page_path(tmp_path)
    assert page in r1.pulled
    assert server.fetch_page_calls == 0  # (1) no-op sync makes ZERO fetch_page calls
    assert page in r2.unchanged and not r2.pushed and not r2.pulled


def test_bumped_revision_count_forces_fetch_and_reconverges_clean(tmp_path: Path) -> None:
    """A marker bump with no actual content change (a no-op remote write) forces one
    detail fetch, classifies clean with no churn, and picks up the fresh markers so the
    NEXT sync is back on the zero-fetch fast path."""
    server = _StatefulServer()
    server.seed(10, "recon", "Recon", "# Recon\n\nnmap etc.")
    with BookStackClient(_CREDS, transport=httpx.MockTransport(server.handler)) as client:
        sync_methodology(tmp_path, client)
        page = _page_path(tmp_path)
        before = markdown_to_page(page.read_text())

        server.touch(server.pages[10])  # e.g. an empty PUT: markers bump, content doesn't
        server.fetch_page_calls = 0
        r = sync_methodology(tmp_path, client)
        assert server.fetch_page_calls == 1  # (2) bumped marker -> must fetch to find out
        assert page in r.unchanged
        assert not r.pushed and not r.pulled and not r.repaired and not r.collisions
        after = markdown_to_page(page.read_text())
        assert after.remote_revision_count == before.remote_revision_count + 1

        server.fetch_page_calls = 0
        r2 = sync_methodology(tmp_path, client)
    assert server.fetch_page_calls == 0  # converged: fresh markers now match the list row
    assert page in r2.unchanged


def test_locally_dirty_page_fetches_and_pushes_stamping_fresh_markers(tmp_path: Path) -> None:
    server = _StatefulServer()
    server.seed(10, "recon", "Recon", "original")
    with BookStackClient(_CREDS, transport=httpx.MockTransport(server.handler)) as client:
        sync_methodology(tmp_path, client)
        page = _page_path(tmp_path)
        before = markdown_to_page(page.read_text())
        page.write_text(page.read_text().replace("original", "edited body"))

        server.fetch_page_calls = 0
        r = sync_methodology(tmp_path, client)
    # (3) locally dirty -> always fetches detail: one in the classification loop (to
    # build the remote counterpart), one more in _apply's own concurrent-edit pre-image
    # fetch — both pre-date this change and are untouched by the delta gate.
    assert server.fetch_page_calls == 2
    assert page in r.pushed
    assert server.pages[10]["markdown"] == "edited body"
    after = markdown_to_page(page.read_text())
    # the push response stamped fresh markers, not the ones from before the push
    assert after.remote_revision_count == before.remote_revision_count + 1
    assert after.remote_updated_at != before.remote_updated_at


def test_legacy_file_without_markers_still_syncs_and_gains_markers(tmp_path: Path) -> None:
    server = _StatefulServer()
    server.seed(10, "recon", "Recon", "steps")
    with BookStackClient(_CREDS, transport=httpx.MockTransport(server.handler)) as client:
        sync_methodology(tmp_path, client)
        page = _page_path(tmp_path)
        legacy = markdown_to_page(page.read_text())
        legacy.remote_updated_at = None
        legacy.remote_revision_count = None
        page.write_text(page_to_markdown(legacy))  # simulate a pre-upgrade file on disk

        server.fetch_page_calls = 0
        r = sync_methodology(tmp_path, client)
    assert server.fetch_page_calls == 1  # (4) markers missing -> fetches detail
    assert page in r.unchanged
    after = markdown_to_page(page.read_text())
    assert after.remote_updated_at is not None and after.remote_revision_count is not None


def test_base64_body_push_is_skipped_with_note(tmp_path: Path) -> None:
    server = _StatefulServer()
    server.seed(10, "recon", "Recon", "original")
    with BookStackClient(_CREDS, transport=httpx.MockTransport(server.handler)) as client:
        sync_methodology(tmp_path, client)
        page = _page_path(tmp_path)
        page.write_text(
            page.read_text().replace(
                "original",
                "edited ![x](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA)",
            )
        )
        r = sync_methodology(tmp_path, client)
    # (5) refused, not pushed — BookStack would rewrite the stored markdown on save
    assert page not in r.pushed
    assert server.pages[10]["markdown"] == "original"
    assert any("base64" in note for _p, note in r.skipped)
