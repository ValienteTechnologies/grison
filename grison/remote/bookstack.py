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

    def fetch_books(self) -> list[dict]:
        data = self._request("GET", "/api/books", params={"count": _LIST_COUNT})
        return data["data"]

    def fetch_pages(self) -> list[dict]:
        data = self._request("GET", "/api/pages", params={"count": _LIST_COUNT})
        return data["data"]

    def fetch_page(self, page_id: int) -> dict:
        return self._request("GET", f"/api/pages/{page_id}")

    def update_page(
        self, page_id: int, *, markdown: str, name: str | None = None, book_id: int | None = None
    ) -> None:
        body: dict = {"markdown": markdown}
        if name is not None:
            body["name"] = name  # so a local title rename actually reaches BookStack
        if book_id is not None:
            body["book_id"] = book_id  # move the page to another book
        self._request("PUT", f"/api/pages/{page_id}", json=body)

    def create_page(self, *, book_id: int, name: str, markdown: str) -> int:
        data = self._request(
            "POST",
            "/api/pages",
            json={"book_id": book_id, "name": name, "markdown": markdown},
        )
        return data["id"]

    def delete_page(self, page_id: int) -> None:
        self._request("DELETE", f"/api/pages/{page_id}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BookStackClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
