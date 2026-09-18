"""End-to-end proofs for D9/BRIEF task C: a book's ``images/`` folder mirrors its
BookStack gallery images (:mod:`grison.adapters.bs_images`, on
:mod:`grison.engine.filesets`), and a page's ``![caption](images/x.png)`` /
``../images/x.png`` line translates to/from BookStack's own absolute gallery URL
(:mod:`grison.adapters.bs_pages`). No caption sync for images (D9: BookStack's
gallery has no caption column at all — the alt text lives only in the page body).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def _read_fm(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return fm, body


def test_new_local_image_and_page_reference_uploads_and_translates_to_the_gallery_url(
    run_grison, bs_server,
):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(
        book_id=book["id"], name="Getting Started", markdown="# Getting Started\n",
    )
    run_grison("sync")  # pull the book/page down first

    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"\x89PNG-fake-bytes")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.write_text(
        "---\ntitle: Getting Started\n---\n\n"
        "![Network diagram](images/diagram.png)\n",
        encoding="utf-8",
    )

    result = run_grison("sync")

    assert "wiki (bs.image" in result.output
    # the image itself was uploaded, anchored to the book's own (only) page
    assert len(bs_server.store.gallery) == 1
    row = bs_server.store.gallery[0]
    assert row["uploaded_to"] == page["id"]
    assert bs_server.store.gallery_bytes[row["id"]] == b"\x89PNG-fake-bytes"
    # the PUSHED page body carries the absolute gallery URL, never the local path
    pushed_markdown = bs_server.store.page(page["id"])["markdown"]
    assert row["url"] in pushed_markdown
    assert "images/diagram.png" not in pushed_markdown
    # the local file itself is untouched — still the authored local-path spelling
    _fm, body = _read_fm(page_path)
    assert "![Network diagram](images/diagram.png)" in body


def test_second_sync_after_image_upload_is_a_noop(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi\n")
    run_grison("sync")
    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"bytes")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.write_text(
        "---\ntitle: Getting Started\n---\n\n![Diagram](images/diagram.png)\n",
        encoding="utf-8",
    )
    run_grison("sync")
    before = page_path.read_text(encoding="utf-8")
    ops_before = len(bs_server.operation_log)

    result = run_grison("sync")

    assert "wiki (bs.image" in result.output
    assert "clean" in [line for line in result.output.splitlines() if "bs.image" in line][0]
    assert page_path.read_text(encoding="utf-8") == before
    assert len(bs_server.operation_log) == ops_before  # zero new mutations


def test_remote_gallery_image_pulls_into_images_dir_and_url_becomes_local_path(
    run_grison, bs_server,
):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(
        book_id=book["id"], name="Network Diagram", markdown="# Network Diagram\n",
    )
    image = bs_server.store.seed_gallery_image(
        name="topology.png", uploaded_to=page["id"], content=b"remote-bytes",
    )
    bs_server.store.edit_page(
        page["id"],
        markdown=f'# Network Diagram\n\n![Topology]({image["url"]})\n',
    )

    result = run_grison("sync")

    assert "wiki (bs.image" in result.output
    local_image = Path.cwd() / "methodology/library/playbook/images/topology.png"
    assert local_image.read_bytes() == b"remote-bytes"
    page_path = Path.cwd() / "methodology/library/playbook/network-diagram.md"
    _fm, body = _read_fm(page_path)
    assert "![Topology](images/topology.png)" in body
    assert image["url"] not in body


def test_chapter_page_image_reference_uses_the_dotdot_spelling(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chapter = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chapter["id"], name="Subdomains",
        markdown="# Subdomains\n",
    )
    run_grison("sync")
    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "scan.png").write_bytes(b"scan-bytes")
    page_path = Path.cwd() / "methodology/library/playbook/recon/subdomains.md"
    page_path.write_text(
        "---\ntitle: Subdomains\n---\n\n![Scan](../images/scan.png)\n",
        encoding="utf-8",
    )

    run_grison("sync")

    pushed_markdown = bs_server.store.page(page["id"])["markdown"]
    row = bs_server.store.gallery[0]
    assert row["url"] in pushed_markdown
    assert "../images/scan.png" not in pushed_markdown


def test_new_image_anchors_to_the_page_that_references_it_not_the_books_first_page(
    run_grison, bs_server,
):
    """BRIEF C: :attr:`~grison.adapters.bs_images.BsImagesAdapter.anchor_for` used to
    be built into the adapter but never populated by ``cli.py`` — every new upload
    fell through to the "book's first page" fallback regardless of which page
    actually referenced it. Fixed by scanning every already-indexed page's body
    (:mod:`grison.markdown.refscan`, not a regex) for ``images/<file>`` embeds
    before building the adapter (``grison/cli.py::_anchor_for_book``)."""
    book = bs_server.store.seed_book(name="Playbook")
    page_a = bs_server.store.seed_page(
        book_id=book["id"], name="Overview", markdown="# Overview\n",
    )
    page_b = bs_server.store.seed_page(
        book_id=book["id"], name="Recon", markdown="# Recon\n",
    )
    assert page_a["id"] < page_b["id"]  # the old first-page fallback would pick page_a
    run_grison("sync")  # pull both pages down first

    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"\x89PNG-fake-bytes")
    recon_path = Path.cwd() / "methodology/library/playbook/recon.md"
    recon_path.write_text(
        "---\ntitle: Recon\n---\n\n![Network diagram](images/diagram.png)\n",
        encoding="utf-8",
    )

    run_grison("sync")

    assert len(bs_server.store.gallery) == 1
    row = bs_server.store.gallery[0]
    assert row["uploaded_to"] == page_b["id"]  # anchored to Recon, not Overview


def test_page_push_reuses_the_per_run_gallery_cache_not_one_fetch_per_page(
    run_grison, bs_server,
):
    """grison/adapters/bs_pages.py::_remote_body_for_push (and the pre-write
    re-fetch guard's own URL localization) used to call ``_gallery_by_name_for_book``
    fresh on every push — a full page-list + gallery-list refetch per page pushed.
    ``fetch_remote`` already cached this per book; the fix (``ctx.gallery_cache``)
    makes create/update/refetch reuse that SAME per-run cache, so the request count
    for a push sync no longer grows with the number of pages pushed."""
    book = bs_server.store.seed_book(name="Playbook")
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"# Page {i}\n")
        for i in range(4)
    ]
    image = bs_server.store.seed_gallery_image(
        name="diagram.png", uploaded_to=pages[0]["id"], content=b"bytes",
    )
    for i, page in enumerate(pages):
        bs_server.store.edit_page(
            page["id"], markdown=f"# Page {i}\n\n![Diagram]({image['url']})\n",
        )
    run_grison("sync")  # pull all 4 pages + the image down (localizes the URL)

    def _edit(i: int) -> None:
        p = Path.cwd() / f"methodology/library/playbook/page-{i}.md"
        p.write_text(p.read_text(encoding="utf-8") + f"\nEdited {i}.\n", encoding="utf-8")

    def _gallery_and_page_list_gets(before: int) -> tuple[int, int]:
        new = bs_server.request_log[before:]
        gallery = [r for r in new if r.method == "GET" and r.path == "/api/image-gallery"]
        pages_ = [r for r in new if r.method == "GET" and r.path == "/api/pages"]
        return len(gallery), len(pages_)

    _edit(0)
    before_one = len(bs_server.request_log)
    run_grison("sync")  # pushes exactly 1 page
    one_gallery, one_pages = _gallery_and_page_list_gets(before_one)

    for i in range(1, 4):
        _edit(i)
    before_many = len(bs_server.request_log)
    run_grison("sync")  # pushes 3 more pages in one run
    many_gallery, many_pages = _gallery_and_page_list_gets(before_many)

    assert one_gallery == many_gallery, (one_gallery, many_gallery)
    assert one_pages == many_pages, (one_pages, many_pages)


def test_undo_refuses_to_guess_the_book_for_an_image_restore_with_no_uploaded_to(
    run_grison, bs_server,
):
    """Same undo-safety fix as ``GwEvidenceAdapter.restore`` (D3):
    ``grison.engine.undo.replay``'s adapter map holds ONE ``BsImagesAdapter`` per
    bare kind string (bound to an empty page-id filter for undo — see
    ``grison/cli.py``'s undo wiring), so ``restore()`` can never trust
    ``self.anchor_page_id``. A preimage with no ``uploaded_to`` at all (a corrupt
    snapshot) must be refused, never silently re-uploaded anchored to a guessed
    page."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi\n")
    run_grison("sync")

    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"\x89PNG-fake-bytes")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.write_text(
        "---\ntitle: Getting Started\n---\n\n![Diagram](images/diagram.png)\n",
        encoding="utf-8",
    )
    run_grison("sync")  # uploads the image
    assert len(bs_server.store.gallery) == 1

    # D1(a): a locally-deleted (indexed) file's remote row is deleted too, whether
    # or not anything still references it -- this is what gives us a delete_remote
    # undo op to corrupt below.
    page_path.write_text("---\ntitle: Getting Started\n---\n\n# Hi\n", encoding="utf-8")
    (images_dir / "diagram.png").unlink()
    run_grison("sync")
    assert bs_server.store.gallery == []

    snapshots_dir = Path.cwd() / ".grison" / "snapshots"
    snapshot_name = sorted(p.name for p in snapshots_dir.iterdir())[-1]
    ops_path = snapshots_dir / snapshot_name / "ops.json"
    ops = json.loads(ops_path.read_text(encoding="utf-8"))
    image_op = next(o for o in ops if o["kind"] == "bs.image" and o["outcome"] == "delete_remote")
    image_op["remote_preimage"]["uploaded_to"] = None
    ops_path.write_text(json.dumps(ops), encoding="utf-8")

    result = run_grison("undo")

    assert result.exit_code == 1, result.output
    assert "diagram.png" in result.output
    assert bs_server.store.gallery == []  # never silently restored into a guessed page


def test_image_reupload_surfaces_as_a_collision_never_a_silent_url_overwrite(
    run_grison, bs_server,
):
    """Before the fix: BsPageAdapter._remote_body_to_local resolved a stored
    absolute gallery URL back to ``images/x.png`` via the LIVE gallery-by-URL map
    only — after a reupload (D9: bytes changing under an unchanged filename
    creates a NEW gallery row, the old one deleted, see grison.adapters.bs_images)
    the page's server-side body still had the OLD, now-orphaned URL, which no
    longer resolved to anything: the page's canonical remote form kept that raw
    URL text, still hashed equal to base (the local file never changed), and the
    page classified PULL — overwriting the local page with a literal URL instead
    of ever being re-pushed with the new one.

    Fixed: canonical_local/canonical_remote fold each image reference's CURRENT
    remote id (BsPageAdapter._fold_image_ids), resolving by the local file's own
    identity (the stable ``images/x.png`` filename) rather than by matching the
    stored URL text — so local no longer equals base once the id underneath that
    filename changes, and the page is never overwritten. This is the SAME
    "COLLISION, not a same-run automatic push" shape a reupload produces for a
    Ghostwriter narrative section/finding (nothing marks the page's OWN stored
    body stale until it is actually re-pushed) — resolving it (``--force-local``,
    "keep my local text") re-pushes with the NEW gallery URL."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi\n")
    run_grison("sync")

    images_dir = Path.cwd() / "methodology/library/playbook/images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"original bytes")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.write_text(
        "---\ntitle: Getting Started\n---\n\n![Network diagram](images/diagram.png)\n",
        encoding="utf-8",
    )
    run_grison("sync")  # uploads the image, pushes the page with the original URL
    original_text = page_path.read_text(encoding="utf-8")
    original_url = bs_server.store.gallery[0]["url"]

    (images_dir / "diagram.png").write_bytes(b"reuploaded bytes, different content")

    result = run_grison("sync")  # bs.image reuploads (new id), bs.page reclassifies

    assert "wiki (bs.page): collision 1" in result.output
    # the core bug this proves fixed: never silently overwritten with the raw URL
    assert page_path.read_text(encoding="utf-8") == original_text
    assert "https://" not in page_path.read_text(encoding="utf-8")
    sidecar = page_path.with_name("getting-started.remote.md")
    assert sidecar.exists()

    new_url = bs_server.store.gallery[0]["url"]
    assert new_url != original_url

    resolve_result = run_grison("sync", "--force-local", str(page_path))

    assert "wiki (bs.page): push 1" in resolve_result.output
    assert page_path.read_text(encoding="utf-8") == original_text  # local text never touched
    pushed_markdown = bs_server.store.gallery and bs_server.store.page(
        next(p["id"] for p in bs_server.store.pages if p["name"] == "Getting Started")
    )["markdown"]
    assert new_url in pushed_markdown
    assert original_url not in pushed_markdown
    assert not sidecar.exists()
