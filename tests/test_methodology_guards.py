"""Track 4 guard tests: BookStack wysiwyg-page safety, the concurrent-edit
name/tags/priority collision guard, the book/chapter rename tripwire, and read-only
structure mirrors with hand-edit detection. All offline against a fake in-memory
BookStack client — no live calls.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from grison.remote import snapshot as snapshot_mod
from grison.remote.bsmap import is_markdown_native, markdown_to_page, page_to_markdown
from grison.remote.methodology import _BSSnapshot, sync_methodology
from grison.workspace import mirrors_path


class FakeBS:
    """In-memory BookStack — same shape as tests/test_methodology.py's double, extended
    additively with editor/raw_html/html support for the wysiwyg guard."""

    def __init__(self) -> None:
        self.books = [{"id": 22, "slug": "mobile", "name": "Mobile", "description": ""}]
        self.chapters: list[dict] = []
        self.shelves: list[dict] = []
        self.pages: dict[int, dict] = {}
        self._next = 500

    def fetch_books(self):
        return self.books

    def fetch_chapters(self):
        return self.chapters

    def fetch_shelves(self):
        return [{k: s[k] for k in ("id", "slug", "name") if k in s} for s in self.shelves]

    def fetch_shelf(self, shelf_id: int):
        return next(s for s in self.shelves if s["id"] == shelf_id)

    def fetch_pages(self):
        return [
            {"id": p["id"], "book_id": p["book_id"], "chapter_id": p.get("chapter_id", 0),
             "slug": p["slug"], "name": p["name"]}
            for p in self.pages.values()
        ]

    def fetch_page(self, page_id: int):
        return dict(self.pages[page_id])  # a fresh dict, like a real HTTP response

    def update_page(self, page_id, *, markdown=None, html=None, name=None, book_id=None,  # type: ignore[no-untyped-def]
                    chapter_id=None, priority=None, tags=None):
        if (markdown is None) == (html is None):
            raise ValueError("update_page requires exactly one of markdown or html")
        p = self.pages[page_id]
        if html is not None:
            # mirrors real BookStack: an html PUT is wysiwyg-authored content — the
            # markdown column goes empty, editor flips to a wysiwyg flavor. This is
            # ONLY ever sent by _BSSnapshot.rollback restoring a wysiwyg pre-image.
            p["raw_html"] = html
            p["html"] = html
            p["markdown"] = ""
            p["editor"] = "wysiwyg"
        else:
            p["markdown"] = markdown
            p["editor"] = "markdown"
        if name is not None:
            p["name"] = name
        if chapter_id is not None:
            ch = next(c for c in self.chapters if c["id"] == chapter_id)
            p["chapter_id"] = chapter_id
            p["book_id"] = ch["book_id"]
        elif book_id is not None:
            p["book_id"] = book_id
            p["chapter_id"] = 0
        if priority is not None:
            p["priority"] = priority
        if tags is not None:
            p["tags"] = tags

    def create_page(self, *, name, markdown, book_id=None, chapter_id=None,  # type: ignore[no-untyped-def]
                    tags=None, priority=None) -> dict:
        i = self._next
        self._next += 1
        if chapter_id is not None:
            book_id = next(c["book_id"] for c in self.chapters if c["id"] == chapter_id)
        rec = {"id": i, "book_id": book_id, "chapter_id": chapter_id or 0,
               "name": name, "slug": name.lower().replace(" ", "-"), "markdown": markdown}
        if tags is not None:
            rec["tags"] = tags
        if priority is not None:
            rec["priority"] = priority
        self.pages[i] = rec
        return dict(rec)

    def delete_page(self, page_id: int) -> None:
        self.pages.pop(page_id, None)

    def seed(self, pid: int, slug: str, name: str, md: str, *,
             book_id: int = 22, chapter_id: int = 0, priority: int | None = None,
             editor: str | None = None, raw_html: str | None = None) -> None:
        rec = {"id": pid, "book_id": book_id, "chapter_id": chapter_id,
               "name": name, "slug": slug, "markdown": md}
        if priority is not None:
            rec["priority"] = priority
        if editor is not None:
            rec["editor"] = editor
        if raw_html is not None:
            rec["raw_html"] = raw_html
        self.pages[pid] = rec

    def add_chapter(self, cid: int, slug: str, name: str, *, book_id: int = 22,
                    priority: int | None = None, description: str = "") -> None:
        c = {"id": cid, "book_id": book_id, "slug": slug, "name": name,
             "description": description}
        if priority is not None:
            c["priority"] = priority
        self.chapters.append(c)


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


def _mutate_after_first_fetch(fake: FakeBS, page_id: int, mutate) -> None:
    """Wrap fake.fetch_page so the FIRST call for ``page_id`` (the batch's up-front
    remote snapshot) returns the pre-mutation state, and ``mutate`` runs right after —
    simulating a concurrent BookStack edit landing mid-batch, between the snapshot and
    this specific page's own apply step (a fresh client.fetch_page call)."""
    calls = {"n": 0}
    orig = fake.fetch_page

    def sneaky(pid):
        calls["n"] += 1
        snap = orig(pid)
        if pid == page_id and calls["n"] == 1:
            mutate()
        return snap

    fake.fetch_page = sneaky  # type: ignore[method-assign]


# --- is_markdown_native (pure function) -----------------------------------------------------


def test_is_markdown_native_editor_flavor() -> None:
    assert is_markdown_native({"editor": "markdown", "markdown": "x", "raw_html": ""})
    assert not is_markdown_native({"editor": "wysiwyg", "markdown": "", "raw_html": "<p>x</p>"})
    assert not is_markdown_native(
        {"editor": "wysiwyg2024", "markdown": "x", "raw_html": "<p>x</p>"}
    )


def test_is_markdown_native_defensive_empty_markdown_with_html() -> None:
    # editor unset/stale but markdown empty while real rendered content exists — refused
    assert not is_markdown_native({"markdown": "", "html": "<p>x</p>"})
    assert is_markdown_native({"markdown": "", "html": ""})  # genuinely empty page, fine
    assert is_markdown_native({"editor": None, "markdown": "content", "raw_html": ""})


# --- WYSIWYG page guard -----------------------------------------------------------------------


def test_wysiwyg_page_pull_is_skipped_not_mirrored(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "", editor="wysiwyg", raw_html="<p>rich content</p>")
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert not page.exists()
    assert page not in r.pulled
    assert any("wysiwyg" in note and "id=100" in note for _p, note in r.skipped)


def test_wysiwyg_stale_local_stub_not_overwritten(tmp_path: Path) -> None:
    """A page already (wrongly, pre-fix) mirrored as an empty stub must never be
    silently pulled-over once detected as wysiwyg — the guard applies on every sync,
    not just the first."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    before = page.read_text()
    fake.pages[100]["editor"] = "wysiwyg"
    fake.pages[100]["raw_html"] = "<p>rich</p>"
    r = sync_methodology(tmp_path, fake)
    assert page.read_text() == before
    assert page not in r.pulled
    assert any("wysiwyg" in note for _p, note in r.skipped)


def test_push_refuses_when_remote_becomes_wysiwyg_mid_sync(tmp_path: Path) -> None:
    """The page was markdown-native when this batch's up-front snapshot ran (so a push
    plan was queued from a genuine local edit), but by the time _apply fetches its own
    fresh pre-image, someone converted it to wysiwyg on BookStack — push must refuse."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited by human"))

    def _flip_to_wysiwyg() -> None:
        fake.pages[100]["editor"] = "wysiwyg"
        fake.pages[100]["raw_html"] = "<p>rich</p>"

    _mutate_after_first_fetch(fake, 100, _flip_to_wysiwyg)
    r = sync_methodology(tmp_path, fake)
    assert page not in r.pushed
    assert any("wysiwyg" in note for _p, note in r.skipped)
    assert fake.pages[100]["markdown"] == "original"  # never overwritten
    assert "edited by human" in page.read_text()  # local edit preserved, not lost


def test_rollback_restores_wysiwyg_page(tmp_path: Path) -> None:
    """Undo snapshots capture editor + raw html so rollback can restore a wysiwyg page
    via the html param — never markdown, which would re-wipe it."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "", editor="wysiwyg", raw_html="<p>original rich content</p>")
    snap = _BSSnapshot()
    snap.before_update(100, fake.fetch_page(100))
    fake.update_page(100, markdown="accidentally converted to markdown")
    assert fake.pages[100]["editor"] == "markdown"
    snap.rollback(fake)
    assert fake.pages[100]["editor"] == "wysiwyg"
    assert fake.pages[100]["raw_html"] == "<p>original rich content</p>"


# --- concurrent-edit collision guard (name/tags/priority) ------------------------------------


def test_concurrent_rename_blocks_push(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    _mutate_after_first_fetch(
        fake, 100, lambda: fake.pages[100].__setitem__("name", "Renamed By Human")
    )
    r = sync_methodology(tmp_path, fake)
    assert page not in r.pushed
    assert page in r.collisions
    assert fake.pages[100]["markdown"] == "original"  # whole push refused, not just the name
    assert fake.pages[100]["name"] == "Renamed By Human"  # concurrent rename preserved
    assert page.with_suffix(".remote.md").exists()
    assert "edited body" in page.read_text()  # local edit preserved, not overwritten


def test_concurrent_tag_change_blocks_push(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    fake.pages[100]["tags"] = [{"name": "draft", "value": ""}]
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    _mutate_after_first_fetch(
        fake, 100,
        lambda: fake.pages[100].__setitem__("tags", [{"name": "reviewed-by-human", "value": ""}]),
    )
    r = sync_methodology(tmp_path, fake)
    assert page not in r.pushed
    assert page in r.collisions
    assert fake.pages[100]["tags"] == [{"name": "reviewed-by-human", "value": ""}]


def test_no_false_collision_when_nothing_races(tmp_path: Path) -> None:
    """Sanity/regression: an ordinary title push with no race still succeeds — the new
    guard must not fire when the pre-image simply matches the up-front snapshot."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("title: Auth", "title: Authentication"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert not r.collisions
    assert fake.pages[100]["name"] == "Authentication"


def test_force_local_bypasses_concurrent_guard(tmp_path: Path) -> None:
    """--force-local means "push my local version regardless of remote drift" — it
    must still bypass this guard exactly like it bypasses the ordinary 3-way check."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    _mutate_after_first_fetch(
        fake, 100, lambda: fake.pages[100].__setitem__("name", "Renamed By Human")
    )
    r = sync_methodology(tmp_path, fake, force_local={page})
    assert page in r.pushed
    assert not r.collisions
    assert fake.pages[100]["markdown"] == "edited body"


# --- book/chapter rename tripwire (bs-structure F7) -------------------------------------------


def test_book_rename_detected_and_blocked(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.books.append({"id": 23, "slug": "unrelated-book", "name": "Unrelated", "description": ""})
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    mobile_dir = tmp_path / "methodology" / "library" / "mobile"
    page = mobile_dir / "auth.md"
    target_dir = tmp_path / "methodology" / "library" / "unrelated-book"
    moved = target_dir / "auth.md"
    page.rename(moved)         # the one page moves into the other book's existing dir
    shutil.rmtree(mobile_dir)  # ...and the old book's dir is genuinely gone (a real `mv`)
    r = sync_methodology(tmp_path, fake)
    assert not r.pushed and not r.pulled and not r.repaired
    assert any(
        "book rename" in e and "mobile" in e and "unrelated-book" in e for e in r.errors
    )
    assert fake.pages[100]["book_id"] == 22  # remote untouched — never reparented onto 23


def test_single_page_move_between_existing_books_still_works(tmp_path: Path) -> None:
    """A real single-page move (not a directory rename) must keep working: the source
    book keeps its OTHER page, so its directory is never gone — no false trip."""
    fake = FakeBS()
    fake.books.append({"id": 23, "slug": "web", "name": "Web", "description": ""})
    fake.seed(100, "auth", "Auth", "body")
    fake.seed(101, "csrf", "CSRF", "other body")  # stays in mobile/
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    new_dir = tmp_path / "methodology" / "library" / "web"
    new_dir.mkdir(parents=True, exist_ok=True)
    moved = new_dir / "auth.md"
    page.rename(moved)
    r = sync_methodology(tmp_path, fake)
    assert not r.errors
    assert moved in r.pushed
    assert fake.pages[100]["book_id"] == 23
    assert fake.pages[101]["book_id"] == 22  # untouched


# --- read-only structure mirrors + edit detection ----------------------------------------------


def test_mirror_first_materialization_has_readonly_header(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty Book", "description": "d"})
    sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "empty-book" / ".book.yml"
    text = mirror.read_text()
    assert text.startswith("# READ-ONLY mirror")
    data = json.loads(mirrors_path(tmp_path).read_text())
    key = "methodology/library/empty-book/.book.yml"
    assert data[key] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_mirror_regenerates_when_untouched(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty Book",
                       "description": "old desc"})
    sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "empty-book" / ".book.yml"
    fake.books[-1]["description"] = "new desc"  # changed on BookStack, mirror untouched locally
    r = sync_methodology(tmp_path, fake)
    assert mirror in r.materialized
    assert "new desc" in mirror.read_text()
    assert not mirror.with_suffix(".remote.yml").exists()
    assert not r.errors


def test_mirror_hand_edit_detected_and_preserved(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty Book",
                       "description": "original"})
    sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "empty-book" / ".book.yml"
    edited = mirror.read_text().replace("original", "hand-edited by user")
    mirror.write_text(edited)
    r = sync_methodology(tmp_path, fake)  # remote unchanged
    assert mirror.read_text() == edited  # untouched — never silently overwritten
    sidecar = mirror.with_suffix(".remote.yml")
    assert sidecar.exists()
    assert "original" in sidecar.read_text()
    assert any("read-only" in e for e in r.errors)


def test_mirror_hand_edit_with_concurrent_remote_change(tmp_path: Path) -> None:
    """The sidecar written on a detected hand-edit must reflect the FRESH remote state,
    not the stale state from when the mirror was first materialized."""
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty Book",
                       "description": "original"})
    sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "empty-book" / ".book.yml"
    mirror.write_text(mirror.read_text().replace("original", "hand-edited"))
    fake.books[-1]["description"] = "changed on bookstack too"
    r = sync_methodology(tmp_path, fake)
    sidecar = mirror.with_suffix(".remote.yml")
    assert "changed on bookstack too" in sidecar.read_text()
    assert any("read-only" in e for e in r.errors)


# --- local==remote short-circuit ruling (hash-schema convergence) -----------------------------


def test_hash_schema_change_restamps_clean_not_collision(tmp_path: Path) -> None:
    """If bs_content_hash's schema ever changes (a new field added), a previously
    stamped base stops matching even though nothing actually changed — sync must
    restamp clean, never report a phantom collision."""
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    lpage = markdown_to_page(page.read_text())
    lpage.synced_hash = "sha256:" + "0" * 64  # stale/foreign-schema base; content untouched
    page.write_text(page_to_markdown(lpage))
    r = sync_methodology(tmp_path, fake)
    assert page in r.repaired
    assert not r.collisions
    assert page in sync_methodology(tmp_path, fake).unchanged  # converged
