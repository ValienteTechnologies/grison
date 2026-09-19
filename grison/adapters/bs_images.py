"""BookStack image gallery, on the shared file-set mechanism (BRIEF D9;
:mod:`grison.engine.filesets`). One :class:`BsImagesAdapter` instance is scoped
to exactly one book and syncs that book's ``images/`` directory against every
gallery-type image ``uploaded_to`` a page belonging to that book (BookStack has
no bulk book-scoped gallery filter — see :mod:`grison.remote.bookstack`'s own
comment — so this adapter fetches every gallery image once and filters
client-side by page-id membership).

No captions (D9's own rule, restated from :mod:`grison.engine.filesets`'s module
docstring): BookStack's gallery has no caption column at all, so
``supports_caption=False`` — an image's alt text lives ONLY in the page body
that references it, is never synced anywhere, and never enters this adapter's
classification.

Anchor page (BRIEF C: "if the gallery API requires a page to attach to, attach
to the first page of the book that references the image and document it" —
this is that documentation): BookStack's upload API requires an ``uploaded_to``
page id, but the gallery model itself has no real notion of "this image belongs
to this page" beyond that one bookkeeping field (confirmed against the lab and
``tests/fixtures/bs-api-docs-26.05.json`` — nothing else reads it) — the id
never becomes visible to a reader and has no effect on which page(s) actually
show the image (a page shows an image because its OWN body links to the gallery
URL, nothing else). So the adapter picks, in order: (1) the first page (by
POSIX path, i.e. deterministic) in this book whose body already references the
file being uploaded, when known (the caller may supply ``anchor_page_id`` from
its own reference scan for exactly this purpose); (2) failing that, an
arbitrary-but-deterministic page already in the book (the first by id) as a
required-but-cosmetic anchor for a brand-new, not-yet-referenced image.

On push, a page's ``![caption](images/x.png)`` (or ``../images/x.png`` inside a
chapter — BRIEF §4/REF-007) becomes the absolute gallery URL BookStack stores
in the page body; on pull, the reverse. That rewrite is :mod:`grison.adapters.
bs_pages`'s job (the page IS the document adapter here), not this module's —
this module only keeps ``images/`` mirroring the gallery rows themselves.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from grison.adapters._bs_common import BSContext
from grison.engine.model import RemoteRecord
from grison.errors import GrisonError
from grison.remote.bookstack import BookStackClient


def _to_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": PurePosixPath(row.get("name") or f"image-{row['id']}.png").name,
        "caption": "",
        "description": "",
        "path": row.get("path", ""),
        "uploaded_to": row.get("uploaded_to"),
    }


def page_ids_in_book(client: BookStackClient, ctx: BSContext, book_id: int) -> frozenset[int]:
    """Every page id belonging to ``book_id`` — a page's own ``book_id``, or its
    chapter's ``book_id`` when it sits inside one. Computed once per sync by the
    caller (findings/wiki CLI wiring) and handed to every book's
    :class:`BsImagesAdapter`, so this isn't refetched per book."""
    chapters_in_book = {c["id"] for c in ctx.chapters if c["book_id"] == book_id}
    out = set()
    for p in client.fetch_pages():
        if p.get("book_id") == book_id or p.get("chapter_id") in chapters_in_book:
            out.add(p["id"])
    return frozenset(out)


@dataclass
class BsImagesAdapter:
    """One book's ``images/`` folder <-> its BookStack gallery images. ``ctx``
    for every method below is a :class:`~grison.adapters._bs_common.BSContext`."""

    book_id: int
    #: ``None`` (only valid for a standalone ``grison undo`` adapter instance,
    #: never for a forward ``sync_fileset`` run) disables the book-membership
    #: filter entirely — ``refetch``'s pre-write/undo re-fetch guard must work for
    #: an id from ANY book, since ``grison.engine.undo.replay``'s adapter map
    #: holds one ``bs.image`` entry total, not one per book (see
    #: :meth:`restore`'s docstring for the identical reasoning on the write side).
    page_ids: frozenset[int] | None
    anchor_page_id: int | None
    #: filename -> the page id that should anchor a brand-new upload of that
    #: file (the first page that already references it — see module docstring);
    #: falls back to ``anchor_page_id`` when a file isn't in here yet.
    anchor_for: dict[str, int] = field(default_factory=dict)
    kind: str = "bs.image"
    supports_caption: bool = False

    def list_remote(self, ctx: BSContext) -> dict[int, RemoteRecord]:
        rows = ctx.client.fetch_gallery_images()
        out: dict[int, RemoteRecord] = {}
        for row in rows:
            if self.page_ids is not None and row.get("uploaded_to") not in self.page_ids:
                continue
            out[row["id"]] = RemoteRecord(id=row["id"], data=_to_data(row))
        return out

    def fetch_body(self, ctx: BSContext, id: int) -> bytes:
        rows = ctx.client.fetch_gallery_images()
        row = next((r for r in rows if r["id"] == id), None)
        if row is None:
            row = ctx.client.fetch_gallery_image(id)
        return ctx.client.download_gallery_image(row["path"])

    def upload(
        self,
        ctx: BSContext,
        *,
        filename: str,
        body: bytes,
        caption: str,
        description: str,
    ) -> RemoteRecord:
        del caption, description  # D9: no caption column at all
        anchor = self.anchor_for.get(filename, self.anchor_page_id)
        if anchor is None:
            raise RuntimeError(
                f"book {self.book_id} has no page at all to anchor a gallery upload to — "
                "create at least one page in this book first"
            )
        row = ctx.client.upload_gallery_image(
            uploaded_to=anchor,
            filename=filename,
            content=body,
            name=filename,
        )
        return RemoteRecord(id=row["id"], data=_to_data(row))

    def update_caption(
        self, ctx: BSContext, id: int, *, caption: str, description: str
    ) -> RemoteRecord:  # pragma: no cover — never called: supports_caption=False
        raise NotImplementedError("BookStack's gallery has no caption column (D9)")

    def delete(self, ctx: BSContext, id: int) -> None:
        ctx.client.delete_gallery_image(id)

    def refetch(self, ctx: BSContext, id: int) -> RemoteRecord | None:
        try:
            row = ctx.client.fetch_gallery_image(id)
        except Exception:  # noqa: BLE001 — gone (404-shaped BookStackError) -> None
            return None
        if self.page_ids is not None and row.get("uploaded_to") not in self.page_ids:
            return None
        return RemoteRecord(id=id, data=_to_data(row))

    def restore(self, ctx: BSContext, preimage: dict[str, Any]) -> RemoteRecord:
        """Re-upload anchored to the preimage's OWN ``uploaded_to`` page — never
        ``self.anchor_page_id``/``self.anchor_for``: ``grison.engine.undo.replay``'s
        adapter map holds one entry per bare kind string, so the instance replaying
        this op may be bound to a different book than this image actually came from
        (same reasoning as :meth:`grison.adapters.gw_evidence.GwEvidenceAdapter.
        restore` — see :func:`grison.engine.filesets._preimage`'s docstring). The
        anchor is cosmetic (module docstring) but must still be a real page id in
        the ORIGINAL book, which ``uploaded_to`` — carried through unchanged in the
        preimage — already is; grison never falls back to ``self.anchor_page_id``
        (this replaying instance's own book may not even be the right one) — a
        preimage with no ``uploaded_to`` at all (a corrupt or pre-existing snapshot)
        is refused outright."""
        anchor = preimage.get("uploaded_to")
        if anchor is None:
            raise GrisonError(
                f"image snapshot for {preimage.get('filename', '(unknown file)')!r} has no "
                "uploaded_to page recorded — cannot determine which book to restore it into"
            )
        body = base64.b64decode(preimage["body_b64"])
        row = ctx.client.upload_gallery_image(
            uploaded_to=anchor,
            filename=preimage["filename"],
            content=body,
            name=preimage["filename"],
        )
        return RemoteRecord(id=row["id"], data=_to_data(row))

    def remote_label(self, data: Any) -> str:
        name = data.get("filename") if isinstance(data, dict) else None
        return f'"{name or "(image)"}"'
