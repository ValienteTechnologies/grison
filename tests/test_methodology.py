"""Phase-9 tests: BookStack methodology 3-way sync via a fake in-memory client."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from grison.remote import snapshot as snapshot_mod
from grison.remote.bsmap import markdown_to_page
from grison.remote.methodology import _BSSnapshot, sync_methodology


class FakeBS:
    """In-memory BookStack. update_page mirrors the real parent-move semantics:
    ``book_id`` re-parents to the book ROOT (ejecting from any chapter), ``chapter_id``
    moves into the chapter — that's what makes the no-eject regression test honest."""

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

    def update_page(self, page_id, *, markdown, name=None, book_id=None,  # type: ignore[no-untyped-def]
                    chapter_id=None, priority=None, tags=None):
        p = self.pages[page_id]
        p["markdown"] = markdown
        if name is not None:
            p["name"] = name
        if chapter_id is not None:  # move into a chapter (implies its book)
            ch = next(c for c in self.chapters if c["id"] == chapter_id)
            p["chapter_id"] = chapter_id
            p["book_id"] = ch["book_id"]
        elif book_id is not None:  # move to a book ROOT — ejects from any chapter
            p["book_id"] = book_id
            p["chapter_id"] = 0
        if priority is not None:
            p["priority"] = priority
        if tags is not None:
            p["tags"] = tags

    def create_page(self, *, name, markdown, book_id=None, chapter_id=None, tags=None) -> dict:  # type: ignore[no-untyped-def]
        i = self._next
        self._next += 1
        if chapter_id is not None:
            book_id = next(c["book_id"] for c in self.chapters if c["id"] == chapter_id)
        rec = {"id": i, "book_id": book_id, "chapter_id": chapter_id or 0,
               "name": name, "slug": name.lower().replace(" ", "-"), "markdown": markdown}
        if tags is not None:
            rec["tags"] = tags
        self.pages[i] = rec
        return dict(rec)

    def delete_page(self, page_id: int) -> None:
        self.pages.pop(page_id, None)

    def seed(self, pid: int, slug: str, name: str, md: str, *,
             book_id: int = 22, chapter_id: int = 0, priority: int | None = None) -> None:
        rec = {"id": pid, "book_id": book_id, "chapter_id": chapter_id,
               "name": name, "slug": slug, "markdown": md}
        if priority is not None:
            rec["priority"] = priority
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
    fake.books.append({"id": 23, "slug": "web", "name": "Web", "description": ""})
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    new_dir = tmp_path / "methodology" / "library" / "web"
    new_dir.mkdir(parents=True, exist_ok=True)
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
    fake.books.append({"id": 23, "slug": "web", "name": "Web", "description": ""})
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    # move the page to another book on BookStack; the local file stays in mobile/
    fake.pages[100]["book_id"] = 23
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert any("moved on BookStack" in why and "web/auth" in why for _p, why in r.drift)
    assert not r.pulled and not r.pushed and not r.repaired
    assert markdown_to_page(page.read_text()).book_id == 22  # local file untouched

    # the user moves the file to match the remote book move
    new_dir = tmp_path / "methodology" / "library" / "web"
    new_dir.mkdir(parents=True, exist_ok=True)
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
    snap.before_update(100, fake.fetch_page(100))
    page.write_text(page.read_text().replace("original", "changed"))
    sync_methodology(tmp_path, fake)
    assert fake.pages[100]["markdown"] == "changed"
    snap.rollback(fake)
    assert fake.pages[100]["markdown"] == "original"


def test_snapshot_rollback_restores_chapter(tmp_path: Path) -> None:
    """The undo journal carries the pre-image's parent — rolling back a chapter move
    puts the page back into its chapter, not just its book."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "body", chapter_id=5)
    snap = _BSSnapshot()
    snap.before_update(100, fake.fetch_page(100))
    fake.update_page(100, markdown="moved", book_id=22)  # ejected to the book root
    assert fake.pages[100]["chapter_id"] == 0
    snap.rollback(fake)
    assert fake.pages[100]["chapter_id"] == 5
    assert fake.pages[100]["markdown"] == "body"


# --- book > chapter > page structure ---------------------------------------------------------


def test_empty_book_materializes(tmp_path: Path) -> None:
    """A book with no pages still becomes a local directory with a .book.yml mirror."""
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty Book",
                       "description": "nothing here yet"})
    r = sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "empty-book" / ".book.yml"
    assert mirror in r.materialized and mirror.exists()
    meta = yaml.safe_load(mirror.read_text())
    assert meta["grison"]["bs"]["book_id"] == 30
    assert meta["name"] == "Empty Book"
    assert meta["description"] == "nothing here yet"
    # idempotent — a second sync rewrites nothing
    assert not sync_methodology(tmp_path, fake).materialized


def test_empty_chapter_materializes(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS", priority=2, description="apple side")
    r = sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "mobile" / "ios" / ".chapter.yml"
    assert mirror in r.materialized and mirror.exists()
    meta = yaml.safe_load(mirror.read_text())
    assert meta["grison"]["bs"] == {"chapter_id": 5, "book_id": 22}
    assert meta["priority"] == 2


def test_dry_run_materializes_nothing(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.books.append({"id": 30, "slug": "empty-book", "name": "Empty", "description": ""})
    r = sync_methodology(tmp_path, fake, dry_run=True)
    assert r.materialized  # reported…
    assert not (tmp_path / "methodology" / "library" / "empty-book").exists()  # …not written


def test_shelf_membership_mirrored(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.shelves.append({"id": 7, "slug": "pentest-ops", "name": "Pentest Ops",
                         "books": [{"id": 22, "slug": "mobile"}]})
    sync_methodology(tmp_path, fake)
    mirror = tmp_path / "methodology" / "library" / "mobile" / ".book.yml"
    assert yaml.safe_load(mirror.read_text())["shelves"] == ["pentest-ops"]


def test_chaptered_page_pulls_into_chapter_dir(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth-ios", "Auth iOS", "steps", chapter_id=5, priority=3)
    r = sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth-ios.md"
    assert page in r.pulled and page.exists()
    parsed = markdown_to_page(page.read_text())
    assert parsed.chapter == "ios" and parsed.chapter_id == 5 and parsed.priority == 3
    assert page in sync_methodology(tmp_path, fake).unchanged  # stable across runs


def test_remote_chapter_move_relocates_local_file(tmp_path: Path) -> None:
    """A page moved into a chapter on BookStack relocates the clean local file —
    chapter moves are content, not drift, and must not ping-pong."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "body")  # book root
    sync_methodology(tmp_path, fake)
    old = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    fake.pages[100]["chapter_id"] = 5  # moved into the chapter on BookStack
    r = sync_methodology(tmp_path, fake)
    new = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth.md"
    assert (old, new) in r.moved and new in r.pulled
    assert not old.exists() and new.exists()
    assert markdown_to_page(new.read_text()).chapter_id == 5
    r2 = sync_methodology(tmp_path, fake)  # converged, no ping-pong
    assert new in r2.unchanged and not r2.pushed and not r2.moved
    assert fake.pages[100]["chapter_id"] == 5



def test_push_does_not_eject_page_from_chapter(tmp_path: Path) -> None:
    """A bare content push of a chaptered page must not send a parent move — the old
    always-send-book_id behavior silently ejected pages to the book root."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "original", chapter_id=5)
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert fake.pages[100]["markdown"] == "edited"
    assert fake.pages[100]["chapter_id"] == 5  # still in its chapter



def test_local_chapter_move_pushes(tmp_path: Path) -> None:
    """Moving the file into a chapter directory pushes a parent move; converged
    afterwards (no ping-pong)."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "body")  # book root
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    moved = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth.md"
    page.rename(moved)
    r = sync_methodology(tmp_path, fake)
    assert moved in r.pushed and not r.drift
    assert fake.pages[100]["chapter_id"] == 5
    assert markdown_to_page(moved.read_text()).chapter_id == 5
    r2 = sync_methodology(tmp_path, fake)
    assert moved in r2.unchanged and not r2.moved


def test_local_move_to_book_root_ejects_deliberately(tmp_path: Path) -> None:
    """A chapter-aware file moved from its chapter dir to the book root IS a stated
    intent — the push ejects the page to the book root."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "body", chapter_id=5)
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth.md"
    moved = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.rename(moved)
    r = sync_methodology(tmp_path, fake)
    assert moved in r.pushed
    assert fake.pages[100]["chapter_id"] == 0
    assert markdown_to_page(moved.read_text()).chapter_id == 0
    assert moved in sync_methodology(tmp_path, fake).unchanged


def test_create_in_chapter_dir(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    sync_methodology(tmp_path, fake)  # materializes mobile/ios/
    newp = tmp_path / "methodology" / "library" / "mobile" / "ios" / "new.md"
    newp.write_text(
        "---\ngrison:\n  kind: methodology\ntitle: Brand New\nbook: mobile\n---\n\nbody"
    )
    r = sync_methodology(tmp_path, fake)
    assert newp in r.created
    parsed = markdown_to_page(newp.read_text())
    assert parsed.page_id is not None and parsed.chapter_id == 5
    assert fake.pages[parsed.page_id]["chapter_id"] == 5


def test_create_in_unknown_chapter_errors(tmp_path: Path) -> None:
    fake = FakeBS()
    newp = tmp_path / "methodology" / "library" / "mobile" / "no-such-chapter" / "new.md"
    newp.parent.mkdir(parents=True)
    newp.write_text(
        "---\ngrison:\n  kind: methodology\ntitle: Brand New\nbook: mobile\n---\n\nbody"
    )
    r = sync_methodology(tmp_path, fake)
    assert not r.created and not fake.pages
    assert any("unknown chapter 'no-such-chapter'" in e for e in r.errors)


def test_nested_too_deep_is_tripwired(tmp_path: Path) -> None:
    fake = FakeBS()
    deep = tmp_path / "methodology" / "library" / "mobile" / "ios" / "extra" / "x.md"
    deep.parent.mkdir(parents=True)
    deep.write_text("---\ngrison:\n  kind: methodology\ntitle: X\nbook: mobile\n---\n\nbody")
    r = sync_methodology(tmp_path, fake)
    assert any(p == deep and "too deep" in note for p, note in r.skipped)
    assert not r.created and not fake.pages


def test_relocation_onto_existing_file_is_tripwired(tmp_path: Path) -> None:
    """A pull-relocation whose target path is already occupied must not overwrite it."""
    fake = FakeBS()
    fake.add_chapter(5, "ios", "iOS")
    fake.seed(100, "auth", "Auth", "body")
    sync_methodology(tmp_path, fake)
    fake.pages[100]["chapter_id"] = 5
    blocker = tmp_path / "methodology" / "library" / "mobile" / "ios" / "auth.md"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("unrelated local notes")
    r = sync_methodology(tmp_path, fake)
    old = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert any(p == old and "relocation target exists" in note for p, note in r.skipped)
    assert old.exists() and blocker.read_text() == "unrelated local notes"


def test_remote_tags_pull_into_frontmatter(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    fake.pages[100]["tags"] = [{"name": "owasp", "value": "A01", "order": 0}]
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    parsed = markdown_to_page(page.read_text())
    assert parsed.tags == [{"name": "owasp", "value": "A01"}]  # extra API keys dropped
    assert page in sync_methodology(tmp_path, fake).unchanged


def test_local_tag_edit_pushes(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    fake.pages[100]["tags"] = [{"name": "draft", "value": ""}]
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert "- draft" in page.read_text()  # value-less tag serializes as a bare string
    page.write_text(page.read_text().replace("- draft", "- reviewed"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert fake.pages[100]["tags"] == [{"name": "reviewed", "value": ""}]
    assert page in sync_methodology(tmp_path, fake).unchanged



def test_remote_reorder_pulls_priority(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body", priority=1)
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert markdown_to_page(page.read_text()).priority == 1
    fake.pages[100]["priority"] = 7  # dragged in the BookStack UI
    r = sync_methodology(tmp_path, fake)
    assert page in r.pulled
    assert markdown_to_page(page.read_text()).priority == 7
    assert page in sync_methodology(tmp_path, fake).unchanged


def test_local_priority_edit_pushes(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body", priority=1)
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("priority: 1", "priority: 9"))
    r = sync_methodology(tmp_path, fake)
    assert page in r.pushed
    assert fake.pages[100]["priority"] == 9
    assert page in sync_methodology(tmp_path, fake).unchanged


# --- on_event progress callback ------------------------------------------------------------


def test_push_emits_event(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    events: list[str] = []
    r = sync_methodology(tmp_path, fake, on_event=events.append)
    assert page in r.pushed
    assert f"push {page.relative_to(tmp_path)}" in events


def test_pull_emits_event(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "body")
    events: list[str] = []
    r = sync_methodology(tmp_path, fake, on_event=events.append)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    assert page in r.pulled
    assert f"pull {page.relative_to(tmp_path)}" in events
    assert any("pulling bookstack state" in e for e in events)
    assert any(e.startswith("bookstack: ") for e in events)


def test_collision_emits_event(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "base")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("base", "LOCAL"))
    fake.pages[100]["markdown"] = "REMOTE"
    events: list[str] = []
    r = sync_methodology(tmp_path, fake, on_event=events.append)
    assert page in r.collisions
    assert f"collision {page.relative_to(tmp_path)} → sidecar written" in events


def test_dry_run_emits_would_prefixed_events(tmp_path: Path) -> None:
    fake = FakeBS()
    fake.seed(100, "auth", "Auth", "original")
    sync_methodology(tmp_path, fake)
    page = tmp_path / "methodology" / "library" / "mobile" / "auth.md"
    page.write_text(page.read_text().replace("original", "edited body"))
    events: list[str] = []
    r = sync_methodology(tmp_path, fake, dry_run=True, on_event=events.append)
    assert page in r.pushed
    assert any(e.startswith("would push ") for e in events)
    assert fake.pages[100]["markdown"] == "original"  # dry-run wrote nothing remotely
