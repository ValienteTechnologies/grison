"""End-to-end proofs for D9/BRIEF task C: a book's ``images/`` folder mirrors its
BookStack gallery images (:mod:`grison.adapters.bs_images`, on
:mod:`grison.engine.filesets`), and a page's ``![caption](images/x.png)`` /
``../images/x.png`` line translates to/from BookStack's own absolute gallery URL
(:mod:`grison.adapters.bs_pages`). No caption sync for images (D9: BookStack's
gallery has no caption column at all — the alt text lives only in the page body).
"""

from __future__ import annotations

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
