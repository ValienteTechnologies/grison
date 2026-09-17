"""Unit tests for the fake BookStack REST API itself — pagination, list-vs-detail row
shapes, write semantics (revision/updated_at bump, html regenerated from markdown),
and failure injection.

The "fidelity" tests load real captured responses from a BookStack 26.05.5 lab
instance (``tests/fixtures/lab-samples/bs-*.json`` — synthetic data, see
``lab/LAB.md`` in the rework repo) and assert the fake reproduces the same row shape
when seeded with the same data.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from grison.remote.bookstack import BookStackClient, BookStackError
from grison.remote.creds import Creds
from tests.fakes.bs_server import FakeBookStack

LAB_SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "lab-samples"

_KEYS_LIST_ROW = {
    "id", "book_id", "chapter_id", "name", "slug", "priority", "draft", "template",
    "created_at", "updated_at", "owned_by", "created_by", "updated_by", "revision_count",
    "editor", "book_slug",
}


def _creds(bs: FakeBookStack) -> Creds:
    return Creds(bs_url="https://fake-bs.invalid", bs_token_id=bs.token_id,
                 bs_token_secret=bs.token_secret)


def _client(bs: FakeBookStack) -> BookStackClient:
    return BookStackClient(_creds(bs), transport=bs.transport)


def test_auth_denied_matches_the_real_lab_server_shape() -> None:
    """Confirmed live against a BookStack 26.05.5 lab instance: ``401`` with
    ``{"error":{"message":"No matching API token was found for the provided
    authorization token","code":401}}``."""
    bs = FakeBookStack(token_id="right", token_secret="right-secret")
    c = BookStackClient(
        Creds(bs_url="https://fake-bs.invalid", bs_token_id="wrong", bs_token_secret="wrong"),
        transport=bs.transport,
    )
    with pytest.raises(BookStackError, match="HTTP 401"):
        c.fetch_books()


def test_pagination_walks_every_page_and_clamps_count(monkeypatch) -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Big Book")
    for i in range(3):
        bs.store.seed_page(book_id=book["id"], name=f"Page {i}")
    monkeypatch.setattr("grison.remote.bookstack._LIST_COUNT", 2)  # force >1 page of results

    pages = _client(bs).fetch_pages()

    assert len(pages) == 3
    assert {p["name"] for p in pages} == {"Page 0", "Page 1", "Page 2"}


def test_list_row_lacks_body_fields_detail_row_has_them() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page", markdown="# Hi\n\nBody")
    c = _client(bs)

    listed = c.fetch_pages()[0]
    assert set(listed) == _KEYS_LIST_ROW
    assert "markdown" not in listed

    detail = c.fetch_page(page["id"])
    assert "markdown" in detail and "html" in detail and "tags" in detail
    assert detail["markdown"] == "# Hi\n\nBody"


def test_update_page_bumps_revision_and_updated_at_on_every_call() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page", markdown="# Hi")
    c = _client(bs)

    r1 = c.update_page(page["id"], markdown="# Hi")  # content-identical PUT
    assert r1["revision_count"] == 2
    r2 = c.update_page(page["id"], markdown="# Hi 2")
    assert r2["revision_count"] == 3
    assert r2["updated_at"] >= r1["updated_at"]


def test_update_page_regenerates_html_from_markdown() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page", markdown="# Hi")
    c = _client(bs)

    updated = c.update_page(page["id"], markdown="# New body")
    assert "New body" in updated["html"]
    assert updated["raw_html"] == updated["html"]


def test_update_page_requires_exactly_one_of_markdown_or_html() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page")
    with pytest.raises(ValueError, match="exactly one"):
        _client(bs).update_page(page["id"], markdown="a", html="b")
    with pytest.raises(ValueError, match="exactly one"):
        _client(bs).update_page(page["id"])


def test_create_page_gives_a_unique_slug_per_book() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    c = _client(bs)
    p1 = c.create_page(name="Same Name", markdown="a", book_id=book["id"])
    p2 = c.create_page(name="Same Name", markdown="b", book_id=book["id"])
    assert p1["slug"] != p2["slug"]
    assert p1["slug"] == "same-name"
    assert p2["slug"] == "same-name-2"


def test_delete_page_moves_it_to_the_recycle_bin() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page")
    c = _client(bs)

    c.delete_page(page["id"])

    assert bs.store.page(page["id"]) is None
    assert len(bs.store.recycle_bin) == 1
    assert bs.store.recycle_bin[0]["deletable_id"] == page["id"]
    assert bs.store.recycle_bin[0]["deletable_type"] == "page"


def test_wysiwyg_page_seeded_with_editor_flag() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(
        book_id=book["id"], name="WYSIWYG", editor="wysiwyg", markdown="",
        raw_html="<p>Authored in the editor.</p>",
    )
    detail = _client(bs).fetch_page(page["id"])
    assert detail["editor"] == "wysiwyg"
    assert detail["markdown"] == ""
    assert detail["raw_html"]


def test_image_gallery_create_list_detail_delete() -> None:
    bs = FakeBookStack()
    # A raw httpx.Client (not BookStackClient, which has no gallery methods yet) — this
    # exercises the fake's multipart handling exactly as a real upload would arrive.
    http = httpx.Client(
        base_url="https://fake-bs.invalid",
        headers={"Authorization": f"Token {bs.token_id}:{bs.token_secret}"},
        transport=bs.transport,
    )

    resp = http.post(
        "/api/image-gallery",
        files={"image": ("diagram.png", b"\x89PNG-fake-bytes")},
        data={"uploaded_to": "17", "type": "gallery"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["name"] == "diagram.png"
    assert created["uploaded_to"] == 17
    assert bs.store.gallery_bytes[created["id"]] == b"\x89PNG-fake-bytes"

    listed = http.get("/api/image-gallery").json()["data"]
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]

    detail = http.get(f"/api/image-gallery/{created['id']}").json()
    assert detail["name"] == "diagram.png"

    del_resp = http.delete(f"/api/image-gallery/{created['id']}")
    assert del_resp.status_code == 204
    assert bs.store.gallery_image(created["id"]) is None


def test_http_500_and_timeout_injection_target_method_and_path() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    c = _client(bs)
    bs.inject_http_500(times=1, method="POST", path=r"/api/pages")

    with pytest.raises(BookStackError, match="HTTP 500"):
        c.create_page(name="X", markdown="x", book_id=book["id"])
    c.fetch_books()  # untargeted GET is unaffected

    bs.inject_timeout(times=1, method="GET", path=r"/api/books")
    with pytest.raises(httpx.TimeoutException):
        c.fetch_books()


def test_operation_log_records_writes_in_order() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    c = _client(bs)
    created = c.create_page(name="X", markdown="x", book_id=book["id"])
    c.update_page(created["id"], markdown="y")
    c.delete_page(created["id"])
    assert [o.name for o in bs.operation_log] == ["create_page", "update_page", "delete_page"]


# --- fidelity: real lab-captured shapes ---------------------------------------


def test_fidelity_page_list_row_field_set_matches_lab_capture() -> None:
    sample = json.loads((LAB_SAMPLES / "bs-pages.json").read_text())["data"][0]
    bs = FakeBookStack()
    book = bs.store.seed_book(id=sample["book_id"], slug=sample["book_slug"], name="x")
    bs.store.seed_page(
        id=sample["id"], book_id=book["id"], chapter_id=sample["chapter_id"] or 0,
        name=sample["name"], slug=sample["slug"], priority=sample["priority"],
        draft=sample["draft"], template=sample["template"], revision_count=1,
        editor=sample["editor"],
    )
    got = _client(bs).fetch_pages()[0]
    assert set(got) == set(sample)


def test_fidelity_book_list_row_field_set_matches_lab_capture() -> None:
    sample = json.loads((LAB_SAMPLES / "bs-books.json").read_text())["data"][0]
    bs = FakeBookStack()
    bs.store.seed_book(name="x")  # only the field SET is being compared, not values
    got = _client(bs).fetch_books()[0]
    assert set(got) == set(sample)


def test_on_request_hook_fires_before_the_nth_matching_request_is_routed() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    page = bs.store.seed_page(book_id=book["id"], name="Page")
    c = _client(bs)

    def mutate() -> None:
        bs.store.page(page["id"])["name"] = "Mutated"

    bs.on_request("GET", r"/api/pages/\d+", mutate, call_number=2)
    first = c.fetch_page(page["id"])["name"]
    second = c.fetch_page(page["id"])["name"]

    assert first == "Page"
    assert second == "Mutated"


def test_request_log_records_reads_and_writes() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    c = _client(bs)
    c.fetch_books()
    c.create_page(name="X", markdown="x", book_id=book["id"])

    kinds = [(r.method, r.path) for r in bs.request_log]
    assert ("GET", "/api/books") in kinds
    assert ("POST", "/api/pages") in kinds
