"""An in-memory fake BookStack REST API, shaped by a real BookStack 26.05.5 instance.

Field sets (list rows vs. detail rows), pagination (``count``/``offset``, capped at
BookStack's documented max of 500), auth header checking, and the revision/updated_at
bump-on-every-write semantics are all modeled after captured responses from the lab
instance (``tests/fixtures/lab-samples/bs-*.json`` — see ``lab/LAB.md`` in the rework
repo) and ``tests/fixtures/bs-api-docs-26.05.json`` (BookStack's own ``/api/docs.json``).

Usage::

    bs = FakeBookStack()
    book = bs.store.seed_book(name="Network Pentest Playbook")
    page = bs.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    client = BookStackClient(creds, transport=bs.transport)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from typing import Any

import httpx

_LIST_COUNT_MAX = 500  # BookStack's own documented per-request cap


class BookStackFakeError(RuntimeError):
    """A 4xx-worthy condition the fake enforces (unknown book/chapter/page, …)."""


@dataclass(frozen=True)
class LoggedOperation:
    """One write the fake executed, in call order."""

    name: str  # "create_page" | "update_page" | "delete_page" | "create_gallery_image" | ...
    variables: dict[str, Any]


@dataclass
class _Injected:
    kind: str  # "http500" | "timeout"
    method: str | None = None
    path: str | None = None

    def matches(self, method: str, path: str) -> bool:
        if self.method is not None and self.method.upper() != method.upper():
            return False
        if self.path is not None and not re.fullmatch(self.path, path):
            return False
        return True


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "page"


def _markdown_stub_html(markdown: str) -> str:
    """A plausible-enough HTML rendering of a markdown body — grison never reads a
    page's ``html``/``raw_html`` on the markdown-native path, only ``markdown``; this
    exists so ``html``/``raw_html`` are never empty-string next to non-empty markdown
    (which would otherwise look like the wysiwyg-with-no-markdown shape to
    ``is_markdown_native``)."""
    lines = [ln for ln in markdown.splitlines() if ln.strip()]
    return "\n".join(f"<p>{ln}</p>" for ln in lines) or "<p></p>"


class BSStore:
    """The in-memory BookStack database backing :class:`FakeBookStack`."""

    def __init__(self) -> None:
        self.shelves: list[dict] = []
        self.books: list[dict] = []
        self.chapters: list[dict] = []
        self.pages: list[dict] = []
        self.gallery: list[dict] = []
        self.gallery_bytes: dict[int, bytes] = {}
        self.recycle_bin: list[dict] = []
        self._next_ids: dict[str, int] = {}
        self.operation_log: list[LoggedOperation] = []

    def _next_id(self, kind: str, *, start: int = 1000) -> int:
        current = self._next_ids.get(kind, start)
        self._next_ids[kind] = current + 1
        return current

    def _log(self, name: str, variables: dict) -> None:
        self.operation_log.append(LoggedOperation(name, dict(variables)))

    # --- seeding -----------------------------------------------------------

    def seed_shelf(self, **fields: Any) -> dict:
        row = {
            "id": self._next_id("shelf"),
            "name": "Untitled shelf",
            "description": "",
            "cover": None,
            "tags": [],
            "created_at": _now(),
            "updated_at": _now(),
            "book_ids": [],  # order matters — never sorted
        }
        row.update(fields)
        row["slug"] = row.get("slug") or _slugify(row["name"])
        self.shelves.append(row)
        return row

    def seed_book(self, **fields: Any) -> dict:
        row = {
            "id": self._next_id("book"),
            "name": "Untitled book",
            "description": "",
            "image_id": None,
            "cover": None,
            "created_at": _now(),
            "updated_at": _now(),
            "owned_by": 1,
            "created_by": 1,
            "updated_by": 1,
        }
        row.update(fields)
        row["slug"] = row.get("slug") or _slugify(row["name"])
        self.books.append(row)
        return row

    def seed_chapter(self, **fields: Any) -> dict:
        row = {
            "id": self._next_id("chapter"),
            "book_id": None,
            "name": "Untitled chapter",
            "description": "",
            "priority": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "owned_by": 1,
            "created_by": 1,
            "updated_by": 1,
        }
        row.update(fields)
        if row["book_id"] is None:
            raise ValueError("seed_chapter requires book_id")
        row["slug"] = row.get("slug") or _slugify(row["name"])
        self.chapters.append(row)
        return row

    def seed_page(self, **fields: Any) -> dict:
        """``editor``/``markdown``/``raw_html`` default to a plain markdown-native
        page; pass ``editor="wysiwyg"`` (and usually ``markdown=""``,
        ``raw_html="<p>...</p>"``) to seed a wysiwyg page grison must skip."""
        if fields.get("book_id") is None:
            raise ValueError("seed_page requires book_id")
        markdown = fields.get("markdown", "# Untitled\n")
        html = _markdown_stub_html(markdown) if markdown else ""
        row = {
            "id": self._next_id("page"),
            "book_id": fields["book_id"],
            "chapter_id": 0,
            "name": "Untitled page",
            "priority": 1,
            "draft": False,
            "template": False,
            "revision_count": 1,
            "editor": "markdown",
            "markdown": markdown,
            "html": html,
            "raw_html": html,
            "tags": [],
            "created_at": _now(),
            "updated_at": _now(),
            "owned_by": 1,
            "created_by": 1,
            "updated_by": 1,
        }
        row.update(fields)
        row["slug"] = row.get("slug") or _slugify(row["name"])
        self.pages.append(row)
        return row

    def seed_gallery_image(self, **fields: Any) -> dict:
        content = fields.pop("content", b"fake-png-bytes")
        row = {
            "id": self._next_id("gallery"),
            "name": "image.png",
            "type": "gallery",
            "uploaded_to": None,
            "created_at": _now(),
            "updated_at": _now(),
            "created_by": 1,
            "updated_by": 1,
        }
        row.update(fields)
        row["path"] = f"/uploads/images/gallery/{row['id']}/{row['name']}"
        row["url"] = f"https://bookstack.example{row['path']}"
        self.gallery.append(row)
        self.gallery_bytes[row["id"]] = content
        return row

    # --- lookups -----------------------------------------------------------

    def book(self, book_id: int) -> dict | None:
        return next((b for b in self.books if b["id"] == book_id), None)

    def chapter(self, chapter_id: int) -> dict | None:
        return next((c for c in self.chapters if c["id"] == chapter_id), None)

    def page(self, page_id: int) -> dict | None:
        return next((p for p in self.pages if p["id"] == page_id), None)

    def shelf(self, shelf_id: int) -> dict | None:
        return next((s for s in self.shelves if s["id"] == shelf_id), None)

    def gallery_image(self, image_id: int) -> dict | None:
        return next((g for g in self.gallery if g["id"] == image_id), None)


# --- REST row projections (list rows and detail rows carry different field sets,
# matching what the lab's real BookStack 26.05.5 returns) ----------------------


def _book_slug_for(store: BSStore, book_id: int) -> str:
    book = store.book(book_id)
    return book["slug"] if book else "book"


def _page_list_row(store: BSStore, page: dict) -> dict:
    return {
        "id": page["id"],
        "book_id": page["book_id"],
        "chapter_id": page["chapter_id"],
        "name": page["name"],
        "slug": page["slug"],
        "priority": page["priority"],
        "draft": page["draft"],
        "template": page["template"],
        "created_at": page["created_at"],
        "updated_at": page["updated_at"],
        "owned_by": page["owned_by"],
        "created_by": page["created_by"],
        "updated_by": page["updated_by"],
        "revision_count": page["revision_count"],
        "editor": page["editor"],
        "book_slug": _book_slug_for(store, page["book_id"]),
    }


def _page_detail_row(store: BSStore, page: dict) -> dict:
    return {
        **_page_list_row(store, page),
        "markdown": page["markdown"],
        "html": page["html"],
        "raw_html": page["raw_html"],
        "tags": page["tags"],
        "comments": {"active": [], "archived": []},
    }


def _book_list_row(book: dict) -> dict:
    return {
        "id": book["id"],
        "slug": book["slug"],
        "name": book["name"],
        "description": book["description"],
        "created_at": book["created_at"],
        "updated_at": book["updated_at"],
        "image_id": book["image_id"],
        "owned_by": book["owned_by"],
        "created_by": book["created_by"],
        "updated_by": book["updated_by"],
        "cover": book["cover"],
    }


def _chapter_list_row(chapter: dict) -> dict:
    return {
        "id": chapter["id"],
        "slug": chapter["slug"],
        "name": chapter["name"],
        "description": chapter["description"],
        "priority": chapter["priority"],
        "book_id": chapter["book_id"],
        "created_at": chapter["created_at"],
        "updated_at": chapter["updated_at"],
        "owned_by": chapter["owned_by"],
        "created_by": chapter["created_by"],
        "updated_by": chapter["updated_by"],
        "book_slug": None,  # filled by caller (needs the books table)
    }


def _shelf_list_row(shelf: dict) -> dict:
    return {
        "id": shelf["id"],
        "slug": shelf["slug"],
        "name": shelf["name"],
        "description": shelf["description"],
        "created_at": shelf["created_at"],
        "updated_at": shelf["updated_at"],
        "cover": shelf["cover"],
    }


def _shelf_detail_row(store: BSStore, shelf: dict) -> dict:
    return {
        **_shelf_list_row(shelf),
        "tags": shelf["tags"],
        "description_html": f"<p>{shelf['description']}</p>" if shelf["description"] else "",
        "books": [_book_list_row(store.book(bid)) for bid in shelf["book_ids"] if store.book(bid)],
    }


class FakeBookStack:
    """An in-memory BookStack REST API exposed as an ``httpx.MockTransport`` handler."""

    def __init__(
        self, *, token_id: str = "test-bs-id", token_secret: str = "test-bs-secret"
    ) -> None:
        self.store = BSStore()
        self.token_id = token_id
        self.token_secret = token_secret
        self._pending: list[_Injected] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def operation_log(self) -> list[LoggedOperation]:
        return self.store.operation_log

    def inject_http_500(
        self, times: int = 1, *, method: str | None = None, path: str | None = None
    ) -> None:
        """The next ``times`` requests matching ``method``/``path`` (a regex fullmatch
        against the URL path; both default to "any request") get HTTP 500 instead of
        being processed — e.g. ``inject_http_500(method="PUT")`` to fail only the next
        page update, sparing the list/detail reads a sync does first."""
        for _ in range(times):
            self._pending.append(_Injected("http500", method=method, path=path))

    def inject_timeout(
        self, times: int = 1, *, method: str | None = None, path: str | None = None
    ) -> None:
        """Same targeting as :meth:`inject_http_500`, but raises
        ``httpx.TimeoutException`` instead."""
        for _ in range(times):
            self._pending.append(_Injected("timeout", method=method, path=path))

    def _pop_injection(self, method: str, path: str) -> _Injected | None:
        for i, item in enumerate(self._pending):
            if item.matches(method, path):
                return self._pending.pop(i)
        return None

    # --- HTTP entry point ----------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        injected = self._pop_injection(request.method, request.url.path)
        if injected is not None and injected.kind == "timeout":
            raise httpx.TimeoutException("fake_bs: injected timeout", request=request)
        if injected is not None and injected.kind == "http500":
            return httpx.Response(500, text="fake_bs: injected internal server error")

        expected_auth = f"Token {self.token_id}:{self.token_secret}"
        if request.headers.get("authorization") != expected_auth:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": "No matching API token was found for the provided "
                        "authorization token",
                        "code": 401,
                    }
                },
            )

        method = request.method
        path = request.url.path
        params = dict(request.url.params)
        try:
            return self._route(method, path, params, request)
        except BookStackFakeError as e:
            return httpx.Response(404, json={"error": {"message": str(e), "code": 404}})

    def _route(  # noqa: PLR0911
        self, method: str, path: str, params: dict, request: httpx.Request
    ) -> httpx.Response:
        store = self.store

        if method == "GET" and path == "/api/books":
            return self._list_response([_book_list_row(b) for b in store.books], params)
        if method == "GET" and (m := re.fullmatch(r"/api/books/(\d+)", path)):
            book = store.book(int(m.group(1)))
            if book is None:
                raise BookStackFakeError(f"book {m.group(1)} not found")
            return httpx.Response(200, json=self._book_detail(book))

        if method == "GET" and path == "/api/chapters":
            rows = []
            for c in store.chapters:
                row = _chapter_list_row(c)
                row["book_slug"] = _book_slug_for(store, c["book_id"])
                rows.append(row)
            return self._list_response(rows, params)
        if method == "GET" and (m := re.fullmatch(r"/api/chapters/(\d+)", path)):
            chapter = store.chapter(int(m.group(1)))
            if chapter is None:
                raise BookStackFakeError(f"chapter {m.group(1)} not found")
            return httpx.Response(200, json=self._chapter_detail(chapter))

        if method == "GET" and path == "/api/shelves":
            return self._list_response([_shelf_list_row(s) for s in store.shelves], params)
        if method == "GET" and (m := re.fullmatch(r"/api/shelves/(\d+)", path)):
            shelf = store.shelf(int(m.group(1)))
            if shelf is None:
                raise BookStackFakeError(f"shelf {m.group(1)} not found")
            return httpx.Response(200, json=_shelf_detail_row(store, shelf))

        if method == "GET" and path == "/api/pages":
            rows = [_page_list_row(store, p) for p in store.pages]
            return self._list_response(rows, params)
        if method == "GET" and (m := re.fullmatch(r"/api/pages/(\d+)", path)):
            page = store.page(int(m.group(1)))
            if page is None:
                raise BookStackFakeError(f"page {m.group(1)} not found")
            return httpx.Response(200, json=_page_detail_row(store, page))
        if method == "POST" and path == "/api/pages":
            body = json.loads(request.content.decode("utf-8"))
            page = self._create_page(body)
            return httpx.Response(200, json=_page_detail_row(store, page))
        if method == "PUT" and (m := re.fullmatch(r"/api/pages/(\d+)", path)):
            body = json.loads(request.content.decode("utf-8"))
            page = self._update_page(int(m.group(1)), body)
            return httpx.Response(200, json=_page_detail_row(store, page))
        if method == "DELETE" and (m := re.fullmatch(r"/api/pages/(\d+)", path)):
            self._delete_page(int(m.group(1)))
            return httpx.Response(204)

        if method == "GET" and path == "/api/image-gallery":
            return self._list_response(list(store.gallery), params)
        if method == "GET" and (m := re.fullmatch(r"/api/image-gallery/(\d+)", path)):
            img = store.gallery_image(int(m.group(1)))
            if img is None:
                raise BookStackFakeError(f"image {m.group(1)} not found")
            return httpx.Response(200, json=img)
        if method == "POST" and path == "/api/image-gallery":
            img = self._create_gallery_image(request)
            return httpx.Response(200, json=img)
        if method == "DELETE" and (m := re.fullmatch(r"/api/image-gallery/(\d+)", path)):
            img = store.gallery_image(int(m.group(1)))
            if img is None:
                raise BookStackFakeError(f"image {m.group(1)} not found")
            store.gallery.remove(img)
            store.gallery_bytes.pop(img["id"], None)
            store._log("delete_gallery_image", {"id": img["id"]})
            return httpx.Response(204)

        if method == "GET" and path == "/api/recycle-bin":
            return self._list_response(list(store.recycle_bin), params)

        return httpx.Response(
            404,
            json={
                "error": {"message": f"{method} {path} not implemented in fake_bs", "code": 404}
            },
        )

    # --- pagination ------------------------------------------------------------

    def _list_response(self, rows: list[dict], params: dict) -> httpx.Response:
        count = min(int(params.get("count", 20)), _LIST_COUNT_MAX)
        offset = int(params.get("offset", 0))
        page_rows = rows[offset : offset + count]
        return httpx.Response(200, json={"data": page_rows, "total": len(rows)})

    # --- book/chapter detail rendering -----------------------------------------

    def _book_detail(self, book: dict) -> dict:
        store = self.store
        contents: list[dict] = []
        for c in sorted(
            (c for c in store.chapters if c["book_id"] == book["id"]),
            key=lambda c: c["priority"],
        ):
            pages = [
                {"id": p["id"], "name": p["name"], "slug": p["slug"], "book_id": p["book_id"],
                 "chapter_id": p["chapter_id"], "priority": p["priority"]}
                for p in sorted(
                    (p for p in store.pages if p["chapter_id"] == c["id"]),
                    key=lambda p: p["priority"],
                )
            ]
            contents.append({**_chapter_list_row(c), "type": "chapter", "pages": pages})
        for p in sorted(
            (p for p in store.pages if p["book_id"] == book["id"] and not p["chapter_id"]),
            key=lambda p: p["priority"],
        ):
            contents.append({**_page_list_row(store, p), "type": "page"})
        return {**_book_list_row(book), "contents": contents}

    def _chapter_detail(self, chapter: dict) -> dict:
        store = self.store
        pages = [
            _page_list_row(store, p)
            for p in sorted(
                (p for p in store.pages if p["chapter_id"] == chapter["id"]),
                key=lambda p: p["priority"],
            )
        ]
        return {**_chapter_list_row(chapter), "pages": pages}

    # --- writes ------------------------------------------------------------

    def _create_page(self, body: dict) -> dict:
        store = self.store
        chapter_id = body.get("chapter_id")
        book_id = body.get("book_id")
        if chapter_id is not None:
            chapter = store.chapter(chapter_id)
            if chapter is None:
                raise BookStackFakeError(f"chapter {chapter_id} not found")
            book_id = chapter["book_id"]
        elif book_id is None or store.book(book_id) is None:
            raise BookStackFakeError(f"book {book_id} not found")
        siblings = [
            p for p in store.pages
            if p["book_id"] == book_id and (p["chapter_id"] or 0) == (chapter_id or 0)
        ]
        slug = self._unique_page_slug(book_id, _slugify(body["name"]))
        page = store.seed_page(
            book_id=book_id,
            chapter_id=chapter_id or 0,
            name=body["name"],
            slug=slug,
            markdown=body.get("markdown", ""),
            tags=body.get("tags") or [],
            priority=body.get("priority") if body.get("priority") is not None
            else len(siblings) + 1,
        )
        store._log("create_page", body)
        return page

    def _unique_page_slug(self, book_id: int, base: str) -> str:
        existing = {p["slug"] for p in self.store.pages if p["book_id"] == book_id}
        slug = base
        n = 1
        while slug in existing:
            n += 1
            slug = f"{base}-{n}"
        return slug

    def _update_page(self, page_id: int, body: dict) -> dict:
        store = self.store
        page = store.page(page_id)
        if page is None:
            raise BookStackFakeError(f"page {page_id} not found")
        if ("markdown" in body) == ("html" in body):
            raise BookStackFakeError("exactly one of markdown or html is required")
        if "markdown" in body:
            page["markdown"] = body["markdown"] or ""
            page["html"] = _markdown_stub_html(page["markdown"])
            page["raw_html"] = page["html"]
        else:
            page["html"] = body["html"] or ""
            page["raw_html"] = page["html"]
        if body.get("name") is not None:
            page["name"] = body["name"]
        if body.get("chapter_id") is not None:
            chapter = store.chapter(body["chapter_id"])
            if chapter is None:
                raise BookStackFakeError(f"chapter {body['chapter_id']} not found")
            page["chapter_id"] = chapter["id"]
            page["book_id"] = chapter["book_id"]
        elif body.get("book_id") is not None:
            if store.book(body["book_id"]) is None:
                raise BookStackFakeError(f"book {body['book_id']} not found")
            page["book_id"] = body["book_id"]
            page["chapter_id"] = 0
        if body.get("priority") is not None:
            page["priority"] = body["priority"]
        if body.get("tags") is not None:
            page["tags"] = body["tags"]
        page["revision_count"] += 1  # bumps on EVERY update_page call, content or not
        page["updated_at"] = _now()
        store._log("update_page", {"id": page_id, **body})
        return page

    def _delete_page(self, page_id: int) -> None:
        store = self.store
        page = store.page(page_id)
        if page is None:
            raise BookStackFakeError(f"page {page_id} not found")
        store.pages.remove(page)
        store.recycle_bin.append(
            {
                "id": store._next_id("deletion"),
                "deleted_by": 1,
                "created_at": _now(),
                "updated_at": _now(),
                "deletable_type": "page",
                "deletable_id": page_id,
                "deletable": dict(page),
            }
        )
        store._log("delete_page", {"id": page_id})

    def _create_gallery_image(self, request: httpx.Request) -> dict:
        content_type = request.headers.get("content-type", "")
        fields, files = _parse_multipart(request.content, content_type)
        if "image" not in files:
            raise BookStackFakeError("image file field is required")
        filename, data = files["image"]
        row = self.store.seed_gallery_image(
            name=fields.get("name") or filename,
            type=fields.get("type") or "gallery",
            uploaded_to=int(fields["uploaded_to"]) if fields.get("uploaded_to") else None,
            content=data,
        )
        self.store._log("create_gallery_image", {k: v for k, v in fields.items()})
        return row


def _parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse a ``multipart/form-data`` body into ``(text_fields, {field: (filename, bytes)})``.

    Multipart is structurally a MIME message, so handing the headers + body to the
    stdlib email parser is the standard trick for decoding it without a third-party
    dependency (grison depends on none for multipart parsing, and this is test-only)."""
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    msg: Message = message_from_bytes(raw)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.get_payload():
        disposition = part.get("Content-Disposition", "")
        m = re.search(r'name="([^"]+)"', disposition)
        if not m:
            continue
        name = m.group(1)
        filename_m = re.search(r'filename="([^"]*)"', disposition)
        if filename_m and filename_m.group(1):
            payload = part.get_payload(decode=True) or b""
            files[name] = (filename_m.group(1), payload)
        else:
            payload = part.get_payload(decode=True)
            fields[name] = (payload or b"").decode("utf-8", errors="replace")
    return fields, files
