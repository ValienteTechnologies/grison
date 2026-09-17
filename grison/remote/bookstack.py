"""BookStack REST API client.

BookStack exposes a REST API at ``{bs_url}/api``. Like Ghostwriter, it sits behind
Cloudflare Access, so every request carries both the CF service-token headers and
BookStack's own token auth (see :mod:`grison.remote.creds`).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from grison.errors import GrisonError
from grison.remote.creds import Creds
from grison.remote.http import BaseHttpClient

# BookStack's own documented per-request maximum for a list endpoint's `count` param.
_LIST_COUNT = 500


class BookStackError(GrisonError, RuntimeError):
    """Raised on a non-2xx HTTP response from the BookStack API."""


class BookStackClient(BaseHttpClient):
    """Thin wrapper over BookStack's REST API."""

    def __init__(
        self,
        creds: Creds,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            creds,
            base_url=creds.bs_url,
            url_setting_name="GRISON_BS_URL",
            headers={"Authorization": f"Token {creds.bs_token_id}:{creds.bs_token_secret}"},
            timeout=timeout,
            transport=transport,
            max_attempts=max_attempts,
            base_delay=base_delay,
            sleep=sleep,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        idempotent: bool | None = None,
    ) -> dict | None:
        """GET/PUT/POST/DELETE all funnel through here. ``idempotent`` defaults to
        "GET only" (see :mod:`grison.remote.http`) — a PUT/POST/DELETE call site
        that's provably safe to retry (none are, today) would pass it explicitly."""
        resp = self._send(method, path, params=params, json=json, idempotent=idempotent)
        if not resp.is_success:
            raise BookStackError(
                f"BookStack request failed: {method} {path} -> "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        if method == "DELETE":
            return None
        return resp.json()

    def _list(self, path: str) -> list[dict]:
        """Fetch *every* row of a list endpoint. BookStack caps a response at ``count``
        rows — without following ``offset`` up to ``total``, a wiki past the cap would
        silently truncate (and previously-synced pages beyond it would look deleted)."""
        rows: list[dict] = []
        while True:
            data = self._request(
                "GET", path, params={"count": _LIST_COUNT, "offset": len(rows)}
            )
            batch = data["data"]
            rows.extend(batch)
            if not batch or len(rows) >= data.get("total", len(rows)):
                return rows

    def fetch_books(self) -> list[dict]:
        return self._list("/api/books")

    def fetch_chapters(self) -> list[dict]:
        return self._list("/api/chapters")

    def fetch_shelves(self) -> list[dict]:
        return self._list("/api/shelves")

    def fetch_shelf(self, shelf_id: int) -> dict:
        return self._request("GET", f"/api/shelves/{shelf_id}")

    def fetch_book(self, book_id: int) -> dict:
        return self._request("GET", f"/api/books/{book_id}")

    def create_book(self, *, name: str, description: str = "") -> dict:
        body: dict = {"name": name}
        if description:
            body["description"] = description
        return self._request("POST", "/api/books", json=body)

    def fetch_chapter(self, chapter_id: int) -> dict:
        return self._request("GET", f"/api/chapters/{chapter_id}")

    def create_chapter(self, *, book_id: int, name: str, description: str = "") -> dict:
        body: dict = {"book_id": book_id, "name": name}
        if description:
            body["description"] = description
        return self._request("POST", "/api/chapters", json=body)

    def delete_chapter(self, chapter_id: int) -> None:
        self._request("DELETE", f"/api/chapters/{chapter_id}")

    def delete_book(self, book_id: int) -> None:
        self._request("DELETE", f"/api/books/{book_id}")

    def fetch_recycle_bin(self) -> list[dict]:
        return self._list("/api/recycle-bin")

    def fetch_pages(self) -> list[dict]:
        return self._list("/api/pages")

    def fetch_page(self, page_id: int) -> dict:
        return self._request("GET", f"/api/pages/{page_id}")

    def update_page(
        self,
        page_id: int,
        *,
        markdown: str | None = None,
        html: str | None = None,
        name: str | None = None,
        book_id: int | None = None,
        chapter_id: int | None = None,
        priority: int | None = None,
        tags: list[dict] | None = None,
    ) -> dict | None:
        # book_id and chapter_id are both *parent moves*: book_id re-parents the page to
        # the book root (ejecting it from any chapter), chapter_id moves it into a chapter.
        # Callers must send at most one, and only when they intend a move.
        #
        # `html` exists ONLY for the wysiwyg-rollback path (grison/remote/methodology.py's
        # _BSSnapshot.rollback): sending markdown ALWAYS regenerates page.html from it and
        # permanently discards any wysiwyg-authored content, so every normal push path must
        # send markdown and only rollback of a wysiwyg pre-image may send html instead —
        # never both.
        if (markdown is None) == (html is None):
            raise ValueError("update_page requires exactly one of markdown or html")
        body: dict = {"html": html} if html is not None else {"markdown": markdown}
        if name is not None:
            body["name"] = name  # so a local title rename actually reaches BookStack
        if chapter_id is not None:
            body["chapter_id"] = chapter_id
        elif book_id is not None:
            body["book_id"] = book_id
        if priority is not None:
            body["priority"] = priority
        if tags is not None:
            body["tags"] = tags
        # the API returns the updated page object — callers use it to restamp
        # updated_at/revision_count without a separate GET.
        return self._request("PUT", f"/api/pages/{page_id}", json=body)

    def create_page(
        self,
        *,
        name: str,
        markdown: str,
        book_id: int | None = None,
        chapter_id: int | None = None,
        tags: list[dict] | None = None,
        priority: int | None = None,
    ) -> dict:
        body: dict = {"name": name, "markdown": markdown}
        if chapter_id is not None:
            body["chapter_id"] = chapter_id
        else:
            body["book_id"] = book_id
        if tags is not None:
            body["tags"] = tags
        if priority is not None:
            body["priority"] = priority
        return self._request("POST", "/api/pages", json=body)

    def delete_page(self, page_id: int) -> None:
        self._request("DELETE", f"/api/pages/{page_id}")
