"""BookStack REST API client.

BookStack exposes a REST API at ``{bs_url}/api``. Like Ghostwriter, it sits behind
Cloudflare Access, so every request carries both the CF service-token headers and
BookStack's own token auth (see :mod:`grison.remote.creds`).
"""

from __future__ import annotations

import httpx

from grison.remote.creds import Creds

_LIST_COUNT = 1000


class BookStackError(RuntimeError):
    """Raised on a non-2xx HTTP response from the BookStack API."""


class BookStackClient:
    """Thin wrapper over BookStack's REST API."""

    def __init__(self, creds: Creds, *, timeout: float = 30.0, transport=None) -> None:
        headers = {
            "Authorization": f"Token {creds.bs_token_id}:{creds.bs_token_secret}",
            **creds.cf_headers(),
        }
        self._client = httpx.Client(
            base_url=creds.bs_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict | None:
        resp = self._client.request(method, path, params=params, json=json)
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

    def fetch_pages(self) -> list[dict]:
        return self._list("/api/pages")

    def fetch_page(self, page_id: int) -> dict:
        return self._request("GET", f"/api/pages/{page_id}")

    def update_page(
        self,
        page_id: int,
        *,
        markdown: str,
        name: str | None = None,
        book_id: int | None = None,
        chapter_id: int | None = None,
        priority: int | None = None,
        tags: list[dict] | None = None,
    ) -> None:
        # book_id and chapter_id are both *parent moves*: book_id re-parents the page to
        # the book root (ejecting it from any chapter), chapter_id moves it into a chapter.
        # Callers must send at most one, and only when they intend a move.
        body: dict = {"markdown": markdown}
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
        self._request("PUT", f"/api/pages/{page_id}", json=body)

    def create_page(
        self,
        *,
        name: str,
        markdown: str,
        book_id: int | None = None,
        chapter_id: int | None = None,
        tags: list[dict] | None = None,
    ) -> dict:
        body: dict = {"name": name, "markdown": markdown}
        if chapter_id is not None:
            body["chapter_id"] = chapter_id
        else:
            body["book_id"] = book_id
        if tags is not None:
            body["tags"] = tags
        return self._request("POST", "/api/pages", json=body)

    def delete_page(self, page_id: int) -> None:
        self._request("DELETE", f"/api/pages/{page_id}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BookStackClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
