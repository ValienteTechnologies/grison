"""Proofs for grison/remote/http.py's shared retry/https/CF-header behavior,
exercised through both real clients against the in-memory fakes (so the proof
covers the actual GhostwriterClient/BookStackClient wiring, not a standalone
double of BaseHttpClient)."""

from __future__ import annotations

import httpx
import pytest

from grison.remote.bookstack import BookStackClient, BookStackError
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient, GhostwriterError
from grison.remote.http import HttpConfigError
from tests.fakes.bs_server import FakeBookStack
from tests.fakes.gw_server import FakeGhostwriter


def _gw_creds(gw: FakeGhostwriter, **kw: object) -> Creds:
    return Creds(gw_url="https://fake-gw.invalid", gw_token=gw.token, **kw)  # type: ignore[arg-type]


def _bs_creds(bs: FakeBookStack, **kw: object) -> Creds:
    return Creds(
        bs_url="https://fake-bs.invalid",
        bs_token_id=bs.token_id,
        bs_token_secret=bs.token_secret,
        **kw,  # type: ignore[arg-type]
    )


# --- a query is retried and succeeds --------------------------------------------


def test_gw_query_is_retried_on_502_and_succeeds() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_status(502, times=1)
    delays: list[float] = []
    c = GhostwriterClient(_gw_creds(gw), transport=gw.transport, sleep=delays.append)
    result = c.whoami()  # does not raise
    assert result["username"]
    assert len(delays) == 1  # exactly one retry happened


def test_bs_get_is_retried_on_503_and_succeeds() -> None:
    bs = FakeBookStack()
    bs.inject_http_status(503, times=1, method="GET", path=r"/api/books")
    delays: list[float] = []
    c = BookStackClient(_bs_creds(bs), transport=bs.transport, sleep=delays.append)
    books = c.fetch_books()  # does not raise
    assert books == []  # no books seeded, but the call succeeded
    assert len(delays) == 1


def test_retry_gives_up_after_max_attempts() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_status(502, times=10)
    c = GhostwriterClient(
        _gw_creds(gw), transport=gw.transport, sleep=lambda _: None, max_attempts=3
    )
    with pytest.raises(GhostwriterError, match="502"):
        c.whoami()


# --- a mutation is NOT retried on a response, even a retryable one -------------


def test_gw_mutation_not_retried_on_500() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_500(times=1)
    delays: list[float] = []
    c = GhostwriterClient(_gw_creds(gw), transport=gw.transport, sleep=delays.append)
    with pytest.raises(GhostwriterError, match="HTTP 500"):
        c.delete_finding(1)
    assert delays == []  # no retry attempted at all


def test_gw_mutation_not_retried_on_502_unlike_a_query() -> None:
    """The stronger proof: 502 IS retryable for an idempotent call (see the query
    test above) — a mutation must still not retry on it, since idempotency (not
    the status code) is what gates the retry."""
    gw = FakeGhostwriter()
    gw.inject_http_status(502, times=1)
    delays: list[float] = []
    c = GhostwriterClient(_gw_creds(gw), transport=gw.transport, sleep=delays.append)
    with pytest.raises(GhostwriterError, match="502"):
        c.delete_finding(1)
    assert delays == []


def test_bs_write_not_retried_on_503() -> None:
    bs = FakeBookStack()
    book = bs.store.seed_book(name="Book")
    bs.inject_http_status(503, times=1, method="POST", path=r"/api/pages")
    delays: list[float] = []
    c = BookStackClient(_bs_creds(bs), transport=bs.transport, sleep=delays.append)
    with pytest.raises(BookStackError, match="503"):
        c.create_page(name="X", markdown="x", book_id=book["id"])
    assert delays == []


# --- 429 honours Retry-After -----------------------------------------------------


def test_gw_429_honours_retry_after_seconds() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_status(429, times=1, retry_after="7")
    delays: list[float] = []
    c = GhostwriterClient(_gw_creds(gw), transport=gw.transport, sleep=delays.append)
    c.whoami()  # does not raise
    assert delays == [7.0]


def test_bs_429_honours_retry_after_seconds() -> None:
    bs = FakeBookStack()
    bs.inject_http_status(429, times=1, method="GET", path=r"/api/books", retry_after="3")
    delays: list[float] = []
    c = BookStackClient(_bs_creds(bs), transport=bs.transport, sleep=delays.append)
    c.fetch_books()  # does not raise
    assert delays == [3.0]


def test_429_without_retry_after_falls_back_to_backoff() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_status(429, times=1)  # no Retry-After header
    delays: list[float] = []
    c = GhostwriterClient(
        _gw_creds(gw), transport=gw.transport, sleep=delays.append, base_delay=0.5
    )
    c.whoami()
    assert len(delays) == 1
    assert delays[0] >= 0.5  # base_delay, not the (absent) Retry-After value


# --- http:// is rejected for both clients ---------------------------------------


def test_ghostwriter_rejects_http_url() -> None:
    creds = Creds(gw_url="http://gw.example", gw_token="t")
    with pytest.raises(HttpConfigError, match="GRISON_GW_URL"):
        GhostwriterClient(creds)


def test_bookstack_rejects_http_url() -> None:
    creds = Creds(bs_url="http://bs.example", bs_token_id="i", bs_token_secret="s")
    with pytest.raises(HttpConfigError, match="GRISON_BS_URL"):
        BookStackClient(creds)


def test_ghostwriter_accepts_https_url() -> None:
    GhostwriterClient(Creds(gw_url="https://gw.example", gw_token="t")).close()


def test_bookstack_accepts_https_url() -> None:
    BookStackClient(
        Creds(bs_url="https://bs.example", bs_token_id="i", bs_token_secret="s")
    ).close()


# --- CF Access headers are sent ---------------------------------------------


def test_gw_sends_cf_access_headers() -> None:
    gw = FakeGhostwriter()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return gw.handle(request)

    creds = _gw_creds(gw, cf_client_id="cid", cf_client_secret="csecret")
    c = GhostwriterClient(creds, transport=httpx.MockTransport(handler))
    c.whoami()
    assert captured[-1].headers["cf-access-client-id"] == "cid"
    assert captured[-1].headers["cf-access-client-secret"] == "csecret"


def test_bs_sends_cf_access_headers() -> None:
    bs = FakeBookStack()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return bs.handle(request)

    creds = _bs_creds(bs, cf_client_id="cid", cf_client_secret="csecret")
    c = BookStackClient(creds, transport=httpx.MockTransport(handler))
    c.fetch_books()
    assert captured[-1].headers["cf-access-client-id"] == "cid"
    assert captured[-1].headers["cf-access-client-secret"] == "csecret"


def test_no_cf_headers_when_not_configured() -> None:
    gw = FakeGhostwriter()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return gw.handle(request)

    c = GhostwriterClient(_gw_creds(gw), transport=httpx.MockTransport(handler))
    c.whoami()
    assert "cf-access-client-id" not in captured[-1].headers
