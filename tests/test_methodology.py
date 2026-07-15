"""Phase-9 tests: BookStack methodology 3-way sync via a fake in-memory client."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.remote import snapshot as snapshot_mod
from grison.remote.bsmap import markdown_to_page
from grison.remote.methodology import _BSSnapshot, sync_methodology


class FakeBS:
    def __init__(self) -> None:
        self.books = [{"id": 22, "slug": "mobile"}]
        self.list_items: list[dict] = []
        self.pages: dict[int, dict] = {}
        self._next = 500

    def fetch_books(self):
        return self.books

    def fetch_pages(self):
        return self.list_items

    def fetch_page(self, page_id: int):
        return self.pages[page_id]

    def update_page(self, page_id, *, markdown, name=None, book_id=None):  # type: ignore[no-untyped-def]
        self.pages[page_id]["markdown"] = markdown
        if name is not None:
            self.pages[page_id]["name"] = name
        if book_id is not None:
            self.pages[page_id]["book_id"] = book_id

    def create_page(self, *, book_id: int, name: str, markdown: str) -> int:
        i = self._next
        self._next += 1
        slug = name.lower().replace(" ", "-")
        self.pages[i] = {
            "id": i, "book_id": book_id, "name": name, "slug": slug, "markdown": markdown,
        }
        self.list_items.append(
            {"id": i, "book_id": book_id, "book_slug": "mobile", "slug": slug, "name": name}
        )
        return i

    def delete_page(self, page_id: int) -> None:
        self.pages.pop(page_id, None)

    def seed(self, pid: int, slug: str, name: str, md: str) -> None:
        self.list_items.append(
            {"id": pid, "book_id": 22, "book_slug": "mobile", "slug": slug, "name": name}
        )
        self.pages[pid] = {"id": pid, "book_id": 22, "name": name, "slug": slug, "markdown": md}


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


def test_pull_and_idempotent(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "# Auth\n\nSteps.")
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert page in r.pulled and page.exists()
    assert "# Auth" in page.read_text()
    assert markdown_to_page(page.read_text()).synced_hash  # merge base stamped
    r2 = sync_methodology(tmp_path, fake)
    assert page in r2.unchanged and not r2.pushed


def test_push_local_edit(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert fake.pages[100]["markdown"] == "edited body"
    assert sync_methodology(tmp_path, fake).unchanged == [page]


def test_create_new_page(tmp_path: Path) -> None:
    fake = FakeBS()
    newp = tmp_path / "methodology" / "library" / "mobile" / "new.md"
    newp.parent.mkdir(parents=True)
    newp.write_text(
        "---\ngrison:\n  kind: methodology\ntitle: Brand New\nbook: mobile\n---\n\nbody"
    )
    r = sync_methodology(tmp_path, fake)
    assert newp in r.created
    page_id = markdown_to_page(newp.read_text()).page_id
    assert page_id is not None and fake.pages[page_id]["markdown"] == "body"


def test_collision_writes_sidecar(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "base")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("base", "LOCAL"))
    fake.pages[100]["markdown"] = "REMOTE"
    r = sync_methodology(tmp_path, fake)
    assert page in r.collisions
    assert "LOCAL" in page.read_text()  # preserved
    assert page.with_suffix(".remote.md").exists()
    assert fake.pages[100]["markdown"] == "REMOTE"  # remote untouched


def test_remote_rename_syncs_normally(tmp_path: Path) -> None:
    """A page renamed on BookStack (slug/title only, same book) is cosmetic — it pulls
    as ordinary content, it is not structure drift."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    fake.list_items[0]["slug"] = "authentication"
    fake.pages[100]["slug"] = "authentication"
    fake.pages[100]["name"] = "Authentication"
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert not r.drift
    assert page in r.pulled
    assert "Authentication" in page.read_text()


def test_local_rename_syncs_normally(tmp_path: Path) -> None:
    """A local filename-only rename (stem change, same book dir) is cosmetic: clean
    content stays unchanged, and an edit afterwards pushes normally."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    renamed = page.parent / "authentication.md"
    page.rename(renamed)
    r = sync_methodology(tmp_path, fake)
    assert not r.drift
    assert renamed in r.unchanged
    renamed.write_text(renamed.read_text().replace("body", "edited body"))
    r2 = sync_methodology(tmp_path, fake)
    assert renamed in r2.pushed
    assert fake.pages[100]["markdown"] == "edited body"


def test_local_book_move_pushes_with_new_book_id(tmp_path: Path) -> None:
    """Moving the file to another book's directory, with BookStack unmoved, is a
    normal push — not drift — and it carries the new book_id both remotely and into
    the local frontmatter (the drift witness must stay truthful)."""
    fake = FakeBS()
    fake.books.append({"id": 23, "slug": "web"})
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    new_dir = tmp_path / "methodology" / "library" / "web"
    new_dir.mkdir(parents=True)
    moved = new_dir / "auth.md"
    page.rename(moved)
    r = sync_methodology(tmp_path, fake)
    assert not r.drift
    assert moved in r.pushed
    assert fake.pages[100]["book_id"] == 23
    assert markdown_to_page(moved.read_text()).book_id == 23


def test_remote_book_move_drifts_until_file_follows(tmp_path: Path) -> None:
    """The remote book moved but the local file hasn't followed → drift, no writes.
    Once the user moves the file to match, the next sync converges with no drift and
    no ping-pong back to the old book."""
    fake = FakeBS()
    fake.books.append({"id": 23, "slug": "web"})
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    # move the page to another book on BookStack; the local file stays in mobile/
    fake.list_items[0]["book_id"] = 23
    fake.list_items[0]["book_slug"] = "web"
    fake.pages[100]["book_id"] = 23
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert any("moved on BookStack" in why and "web/auth" in why for _p, why in r.drift)
    assert not r.pulled and not r.pushed and not r.repaired
    assert markdown_to_page(page.read_text()).book_id == 22  # local file untouched

    # the user moves the file to match the remote book move
    new_dir = tmp_path / "methodology" / "library" / "web"
    new_dir.mkdir(parents=True)
    moved = new_dir / "auth.md"
    page.rename(moved)
    r2 = sync_methodology(tmp_path, fake)
    assert not r2.drift
    assert moved in r2.repaired
    assert markdown_to_page(moved.read_text()).book_id == 23

    # converged — no ping-pong on the following run
    r3 = sync_methodology(tmp_path, fake)
    assert not r3.drift and not r3.pushed and not r3.repaired
    assert moved in r3.unchanged
    assert fake.pages[100]["book_id"] == 23  # never moved back


def test_duplicate_page_id_skipped(tmp_path: Path) -> None:
    """Two files claiming the same page_id: both surfaced in skipped, neither
    synced, and the remote page is never written."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    dup = page.parent / "auth-copy.md"
    dup.write_text(page.read_text())
    r = sync_methodology(tmp_path, fake)
    dup_notes = {p for p, note in r.skipped if "duplicate page_id" in note}
    assert dup_notes == {page, dup}
    assert not r.pushed and not r.pulled and not r.repaired and not r.created
    assert fake.pages[100]["markdown"] == "body"  # remote untouched


def test_failing_push_not_counted(tmp_path: Path) -> None:
    """A push that raises must not land in result.pushed alongside the error —
    it would otherwise be double-counted (green count + error)."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    fake.update_page = _boom  # type: ignore[method-assign]
    r = sync_methodology(tmp_path, fake)
    assert page not in r.pushed
    assert any("boom" in e for e in r.errors)
    assert fake.pages[100]["markdown"] == "original"  # never actually written


def test_artifact_scan_flags_corruption(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "clean")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("clean", 'leaked <span class="citation-1">x</span>'))
    r = sync_methodology(tmp_path, fake)
    assert any("citation" in what for _p, what in r.artifacts)
    # the guardrail REFUSES the push — corruption never reaches BookStack
    assert r.pushed == [] and fake.pages[100]["markdown"] == "clean"


def test_title_rename_reaches_bookstack(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("title: Auth", "title: Authentication"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert fake.pages[100]["name"] == "Authentication"  # the rename actually reached BookStack


def test_checklists_are_local_only(tmp_path: Path) -> None:
    """methodology/checklists/ is never synced (the 2×2's local-only fourth cell)."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    chk = tmp_path / "methodology" / "checklists" / "acme" / "auth.md"
    chk.parent.mkdir(parents=True)
    chk.write_text(
        "---\ngrison:\n  kind: methodology\ntitle: Auth\nbook: mobile\n---\n\nengagement work"
    )
    r = sync_methodology(tmp_path, fake)
    # the library page pulls; the checklist copy is invisible to sync and untouched
    all_paths = r.pulled + r.pushed + r.created + [p for p, _ in r.skipped]
    assert chk not in all_paths
    assert chk.read_text().endswith("engagement work")
    assert len(fake.pages) == 1  # no page created from the checklist copy


def test_snapshot_rollback(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    snap = _BSSnapshot()
    snap.before_update(100, fake.pages[100]["markdown"])
    page.write_text(page.read_text().replace("original", "changed"))
    sync_methodology(tmp_path, fake)
    assert fake.pages[100]["markdown"] == "changed"
    snap.rollback(fake)
    assert fake.pages[100]["markdown"] == "original"
