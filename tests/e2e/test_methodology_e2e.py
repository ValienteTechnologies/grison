"""End-to-end wiki (BookStack) sync scenarios, on the sync engine (ENGINE.md) —
CLI + on-disk files + ``bs_server``'s inspection API only. No import from
``grison.engine``/``grison.adapters`` internals, no assertion on private state-file
contents (only ``.grison/index.json``, which is tracked, not private).

Format v2 (BRIEF D3/D4): a page file carries NO machine fields at all — no id, no
``grison:`` block, no ``book``/``chapter`` frontmatter keys. Identity lives in
``.grison/index.json``; a page's book/chapter come from its directory only.

Every ``run_grison("sync")`` here also runs the findings phase (engine-managed —
see ``tests/e2e/test_findings_e2e.py``), which is clean by default since these
tests never seed any library/reported findings. These tests still assert the
wiki phase's own success signals (its own summary line, the BookStack operation
log, the files on disk) rather than assuming anything about the overall exit
code, since a test here is about the wiki phase specifically.

Tests changed on purpose (BRIEF D3/D6/D7, ENGINE.md "Identity"/"classification
table"/apply loop §8) vs. the module this replaces (``grison/remote/methodology.py``,
tested by the pre-engine version of this file):

- No ids/``grison:`` block, no ``book``/``chapter`` keys in the document — D3/D4.
  Identity is ``.grison/index.json`` only.
- A local delete of a synced, clean page now DELETES it remotely, under the change
  guard (classify.py row 6) — was "re-pulled" (the old module never offered a
  delete-remote path at all).
- A remote-gone page with a clean local copy is now DELETED locally (classify.py row
  8) — was "orphan skip".
- Copying a file can no longer produce a "duplicate identity" (there is no id in the
  file to duplicate) — a copy is an ordinary CREATE (ENGINE.md "Identity").
- A rename/move is resolved by content-identity pairing (ENGINE.md "Identity"), not
  by "directory is book identity, page is content" doctrine — a moved-and-unedited
  file is a pure index move (no write), a moved-and-edited file is MOVE+EDIT (same
  remote record, updated in place, reparented); there is no more "structure-drift"
  trip-wire or book-rename block — a moved file (even across books) simply reparents.
- A remote-side parent change (chapter/book move made on BookStack) now RELOCATES the
  local file automatically on pull (the PULL-side counterpart of a local move) —
  there is no more "structure-drift, converges only once the user moves the file by
  hand" state.
- A collision resolved by ``--force-remote``/``--force-local`` now clears the
  ``.remote.md`` sidecar (ENGINE.md §8) — was left stale.
- A page/chapter missing from the live lists but present in ``/api/recycle-bin`` is a
  SKIP ("in the BookStack recycle bin, recoverable there"), never a delete/orphan —
  BRIEF B.
- The literal-artifact "corruption" scan and its pre-existing-cruft grandfathering are
  REMOVED by construction (BRIEF D5/D6) — page-body hygiene now lives entirely in the
  validator (WIKI-…); a page that fails a WIKI rule is INVALID locally (never pushed);
  a bad REMOTE page still pulls (the gate blocks pushes, not pulls) and then shows up
  invalid in ``grison validate``.
- The mass-change guard now covers bulk PULLs too, not just push/create (ENGINE.md
  "change guard": "D = planned local overwrites... same rule independently for D").
- A too-deeply-nested local file (book/chapter/sub/x.md) is no longer a sync-time
  trip-wire message — the adapter simply never scans past chapter depth (BookStack
  itself has no deeper nesting); the file's presence is instead a validator failure
  (WS-003, "unexpected entry directly inside a chapter directory") ``grison validate``
  reports.
- A remote-side move onto an already-occupied local path is now a genuine tripwire at
  apply time too (never a silent overwrite) — kept, adapted to the new relocation
  mechanism.
- BRIEF's wiki-location section decides "new book/chapter directories ARE allowed" —
  a new page inside a not-yet-existing chapter directory (under a KNOWN book) now
  auto-creates the chapter (see ``test_new_local_chapter_directory_creates_the_
  chapter_on_bookstack``), replacing the old module's "unknown chapter dir errors
  loudly" trip-wire, which the new create-on-demand behavior makes obsolete/wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from grison.index import Index


def _read_fm(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return fm, body


def _write_page_file(
    path: Path,
    *,
    title: str,
    body: str,
    priority: int | None = None,
    tags: list | None = None,
) -> None:
    """Author (or hand-edit) a format-v2 page file — title/priority/tags only, no
    machine fields; book/chapter come from ``path``'s own directory (D4)."""
    fm: dict = {"title": title}
    if priority is not None:
        fm["priority"] = priority
    if tags:
        fm["tags"] = tags
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False).strip() + f"\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _indexed_id(workspace: Path, rel: str) -> int:
    rec = Index.load(workspace).get(rel)
    assert rec is not None, f"{rel} is not indexed"
    return rec.id


# ---------------------------------------------------------------------------
# Basic pull / push / clean
# ---------------------------------------------------------------------------


def test_first_sync_pulls_everything(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chapter = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"],
        chapter_id=chapter["id"],
        name="Getting Started",
        markdown="# Getting Started\n\nHello.",
    )

    result = run_grison("sync")

    assert "wiki (bs.page): pull_new 1" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/recon/getting-started.md"
    assert page_path.exists()
    fm, body = _read_fm(page_path)
    assert fm == {"title": "Getting Started", "priority": 1}  # priority defaults to 1 on the fake
    assert body == "# Getting Started\n\nHello."
    assert (page_path.parent.parent / ".book.yml").exists()
    assert (page_path.parent / ".chapter.yml").exists()
    assert bs_server.operation_log == []  # pull never mutates BookStack
    assert (
        _indexed_id(Path.cwd(), "methodology/library/playbook/recon/getting-started.md")
        == page["id"]
    )


def test_second_sync_is_a_noop(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    before = page_path.read_text(encoding="utf-8")
    before_mtime = page_path.stat().st_mtime_ns

    result = run_grison("sync")

    assert "wiki (bs.page): clean 1" in result.output
    assert page_path.read_text(encoding="utf-8") == before
    assert page_path.stat().st_mtime_ns == before_mtime
    assert bs_server.operation_log == []  # zero mutations on the fake, both syncs


def test_local_edit_pushes(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, title="Getting Started", body="# Hi\n\nEdited by hand.")

    result = run_grison("sync")

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Hi\n\nEdited by hand."
    names = [o.name for o in bs_server.operation_log]
    assert names == ["update_page"]


def test_remote_edit_pulls(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    bs_server.store.edit_page(page["id"], markdown="# Hi\n\nChanged on BookStack.")

    result = run_grison("sync")

    assert "wiki (bs.page): pull 1" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nChanged on BookStack."
    assert bs_server.operation_log == []  # a pull never writes to BookStack


def test_collision_surfaced_never_overwritten_then_force_flags(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, title="Getting Started", body="# Hi\n\nLocal change.")
    bs_server.store.edit_page(page["id"], markdown="# Hi\n\nRemote change.")

    result = run_grison("sync")

    assert "collision" in result.output
    # local is untouched; the remote side is surfaced in a sidecar, never overwritten
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nLocal change."
    sidecar = page_path.with_suffix(".remote.md")
    assert sidecar.exists()
    assert "Remote change." in sidecar.read_text(encoding="utf-8")

    result_force_local = run_grison("sync", "--force-local", str(page_path))
    assert "wiki (bs.page): push 1" in result_force_local.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Hi\n\nLocal change."
    # tests changed on purpose: stale collision sidecars are cleared (ENGINE.md §8)
    assert not sidecar.exists()


def test_force_remote_resolves_collision(run_grison, bs_server):
    """tests changed on purpose (ENGINE.md §8, D6 'one collision-sidecar policy,
    stale sidecars cleared everywhere'): the old module left the ``.remote.md``
    sidecar behind after a forced resolution; the engine clears it as soon as the
    record is no longer in collision."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, title="Getting Started", body="# Hi\n\nLocal change.")
    bs_server.store.edit_page(page["id"], markdown="# Hi\n\nRemote change.")
    run_grison("sync")  # first surfaces the collision + sidecar

    result = run_grison("sync", "--force-remote", str(page_path))

    assert "wiki (bs.page): pull 1" in result.output
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nRemote change."  # the tracked file itself IS resolved
    assert not page_path.with_suffix(".remote.md").exists()  # sidecar cleared, not left stale


def test_new_local_page_creates(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page_path = Path.cwd() / "methodology" / "library" / "playbook" / "new-page.md"
    _write_page_file(page_path, title="New Page", body="New page body text.")

    result = run_grison("sync")

    assert "wiki (bs.page): create 1" in result.output
    fm, body = _read_fm(page_path)
    assert fm == {"title": "New Page", "priority": 1}  # priority assigned by BookStack on create
    assert body == "New page body text."
    created = [p for p in bs_server.store.pages if p["book_id"] == book["id"]]
    assert len(created) == 1
    assert created[0]["markdown"] == "New page body text."
    assert _indexed_id(Path.cwd(), "methodology/library/playbook/new-page.md") == created[0]["id"]


def test_undo_of_a_page_create_restores_the_authors_original_file(run_grison, bs_server) -> None:
    """Coordinator correction: undoing a CREATE restores the author's own pre-push
    bytes (not BookStack's post-create rewrite, which assigns ``priority`` — see
    ``test_new_local_page_creates`` above — and not a bare delete, which would lose
    the author's words entirely) and un-indexes the path."""
    book = bs_server.store.seed_book(name="Playbook")
    page_path = Path.cwd() / "methodology" / "library" / "playbook" / "new-page.md"
    _write_page_file(page_path, title="New Page", body="New page body text.")
    original_text = page_path.read_text(encoding="utf-8")

    run_grison("sync")
    assert page_path.read_text(encoding="utf-8") != original_text  # rewritten (priority added)

    result = run_grison("undo")

    assert result.exit_code == 0
    assert page_path.read_text(encoding="utf-8") == original_text  # restored, not deleted
    assert not any(p["book_id"] == book["id"] for p in bs_server.store.pages)
    assert Index.load(Path.cwd()).get("methodology/library/playbook/new-page.md") is None


def test_undo_of_a_create_prints_the_kept_as_new_message_and_status_shows_it_new(
    run_grison, bs_server
) -> None:
    """Item 13 (fix-fin1): undoing a CREATE keeps the author's own file on disk by
    design (their words are never deleted by an undo) — it just un-indexes it, so
    the very next ``grison sync`` would recreate it. Before this fix, ``grison
    undo`` said nothing about that, leaving the operator to discover it by
    surprise on the next sync. Now it prints exactly what state the file is in,
    and ``grison status`` (run right after, no sync in between) already shows it
    as ``new`` — a direct consequence of the index/state entries being removed,
    which this test locks in rather than assumes."""
    book = bs_server.store.seed_book(name="Playbook")
    page_path = Path.cwd() / "methodology" / "library" / "playbook" / "new-page.md"
    _write_page_file(page_path, title="New Page", body="New page body text.")
    run_grison("sync")

    result = run_grison("undo")

    assert result.exit_code == 0, result.output
    assert (
        "methodology/library/playbook/new-page.md: local file kept as new; "
        "delete it or the next sync recreates it" in result.output
    )
    assert not any(p["book_id"] == book["id"] for p in bs_server.store.pages)

    status = run_grison("status")
    assert "methodology: new 1" in status.output


def test_copying_a_synced_page_creates_a_new_remote_page(run_grison, bs_server):
    """tests changed on purpose (D3): there is no id in the document any more, so
    copying a file can no longer collide on identity — it is an ordinary CREATE."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology/library/playbook/notes.md"
    copy = path.parent / "notes-copy.md"
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    result = run_grison("sync")

    assert "wiki (bs.page): clean 1, create 1" in result.output
    assert len(bs_server.store.pages) == 2


def test_local_delete_of_a_clean_page_deletes_it_remotely(run_grison, bs_server):
    """tests changed on purpose (classify.py row 6): a local delete of a clean,
    indexed page now deletes the remote page too (was: silently re-pulled — the old
    module had no delete-remote path at all)."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.unlink()

    result = run_grison("sync")

    assert "wiki (bs.page): delete_remote 1" in result.output
    assert bs_server.store.page(page["id"]) is None
    assert any(d["deletable_id"] == page["id"] for d in bs_server.store.recycle_bin)


def test_remote_delete_of_a_clean_page_deletes_it_locally(run_grison, bs_server):
    """tests changed on purpose (classify.py row 8): a remote-gone page with a clean
    local copy is now deleted locally (was: 'orphan skip', left on disk forever)."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    bs_server.store.pages.remove(bs_server.store.page(page["id"]))  # gone, not recycled

    result = run_grison("sync")

    assert "wiki (bs.page): delete_local 1" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    assert not page_path.exists()
    assert Index.load(Path.cwd()).get("methodology/library/playbook/getting-started.md") is None


def test_dry_run_writes_nothing(run_grison, bs_server, tmp_path):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("sync", "--dry-run")

    assert "pull" in result.output.lower()
    lib = Path.cwd() / "methodology" / "library"
    assert not any(lib.rglob("*.md"))  # nothing landed on disk
    assert bs_server.operation_log == []


def test_malformed_local_file_does_not_stop_others(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    good = bs_server.store.seed_page(book_id=book["id"], name="Good Page", markdown="# Good")
    run_grison("sync")
    good_path = Path.cwd() / "methodology/library/playbook/good-page.md"
    _write_page_file(good_path, title="Good Page", body="# Good\n\nEdited.")
    broken_path = Path.cwd() / "methodology" / "library" / "playbook" / "broken.md"
    broken_path.write_text("not frontmatter at all", encoding="utf-8")

    result = run_grison("sync")

    assert bs_server.store.page(good["id"])["markdown"] == "# Good\n\nEdited."
    assert "broken.md" in result.output
    assert "invalid" in result.output.lower()  # WIKI-014 (bad frontmatter) via the validation gate


def test_server_failure_on_one_page_does_not_stop_the_other(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    p1 = bs_server.store.seed_page(book_id=book["id"], name="Page One", markdown="# One")
    p2 = bs_server.store.seed_page(book_id=book["id"], name="Page Two", markdown="# Two")
    run_grison("sync")
    path1 = Path.cwd() / "methodology/library/playbook/page-one.md"
    path2 = Path.cwd() / "methodology/library/playbook/page-two.md"
    _write_page_file(path1, title="Page One", body="# One\n\nEdited.")
    _write_page_file(path2, title="Page Two", body="# Two\n\nEdited.")
    bs_server.inject_http_500(times=1, method="PUT")  # only a page update fails, not the reads

    result = run_grison("sync")

    succeeded = [
        p for p in (p1, p2) if bs_server.store.page(p["id"])["markdown"].endswith("Edited.")
    ]
    assert len(succeeded) == 1  # exactly one push got through; the other was isolated
    assert "failed" in result.output.lower()


def test_mass_change_guard_withholds_pushes_and_announces_it(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    # a 6-page first sync is fine (PULL_NEW never trips the guard — see
    # test_mass_change_guard_withholds_pulls_too's own note); editing all 6 locally
    # then trips the PUSH (W) side.
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"Body {i}.")
        for i in range(6)
    ]
    run_grison("sync")
    for i, _p in enumerate(pages):
        path = Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md"
        _write_page_file(path, title=f"Page {i}", body=f"Body {i}.\n\nEdited.")

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on bs.page — writes withheld." in result.output
    assert bs_server.operation_log == []  # every push was withheld, none reached BookStack


def test_mass_change_guard_withholds_pulls_too(run_grison, bs_server):
    """tests changed on purpose (ENGINE.md change guard applies to D = pulls too, not
    just W = remote writes — the old module only ever guarded push/create). D only
    covers PULL/DELETE_LOCAL (an OVERWRITE/removal of something already local) —
    PULL_NEW deliberately does not count (nothing local to lose), so a first sync of
    a brand-new, arbitrarily large wiki is never itself withheld; six pages already
    pulled and then all edited remotely is what actually trips the D side."""
    book = bs_server.store.seed_book(name="Playbook")
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"Body {i}.")
        for i in range(6)
    ]
    run_grison("sync")  # a 6-page first sync is NOT withheld (all PULL_NEW)
    for i, pg in enumerate(pages):
        bs_server.store.edit_page(pg["id"], markdown=f"Body {i} changed on BookStack.")

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on bs.page — writes withheld." in result.output
    for i in range(6):
        path = Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md"
        assert "changed on BookStack" not in path.read_text(encoding="utf-8")


def test_force_remote_on_a_withheld_deletion_restores_it_not_deletes_the_remote(
    run_grison, bs_server
):
    """Item 11 (fix-fin1): before this fix, ``--force-remote <path>`` on a
    locally-deleted, guard-withheld page only exempted it from WITHHELD while
    leaving the outcome DELETE_REMOTE — the remote copy still got deleted, the
    opposite of what "the remote wins" means. Now it restores the file locally
    instead (a PULL) and the remote record survives."""
    book = bs_server.store.seed_book(name="Playbook")
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"Body {i}.")
        for i in range(6)
    ]
    run_grison("sync")
    paths = [Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md" for i in range(6)]
    for path in paths:
        path.unlink()  # delete all 6 locally -> would DELETE_REMOTE all 6, trips the guard

    result = run_grison("sync", "--force-remote", str(paths[0]))

    assert "MASS-CHANGE GUARD tripped on bs.page — writes withheld." in result.output
    assert paths[0].exists()  # restored, not deleted
    assert paths[0].read_text(encoding="utf-8").strip().endswith("Body 0.")
    for path in paths[1:]:
        assert not path.exists()  # still deleted locally — withheld, no write either way
    # every remote page survives — the forced one because it was restored, the
    # rest because the guard withheld their deletion.
    for pg in pages:
        assert bs_server.store.page(pg["id"]) is not None


def test_force_local_on_a_withheld_deletion_recreates_it_not_deletes_it_locally(
    run_grison, bs_server
):
    """The mirror image: a remote-deleted, guard-withheld page named on
    ``--force-local`` used to only exempt it from WITHHELD while leaving the
    outcome DELETE_LOCAL — the local file still got deleted, the opposite of
    "the local side wins". Now it recreates the record remotely (CREATE) and
    the local file survives untouched."""
    book = bs_server.store.seed_book(name="Playbook")
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"Body {i}.")
        for i in range(6)
    ]
    run_grison("sync")  # pulls all 6
    for pg in pages:
        bs_server.store.pages.remove(bs_server.store.page(pg["id"]))  # gone, not recycled
    paths = [Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md" for i in range(6)]

    result = run_grison("sync", "--force-local", str(paths[0]))

    assert "MASS-CHANGE GUARD tripped on bs.page — writes withheld." in result.output
    for path in paths:
        assert path.exists()  # NONE deleted locally — forced one recreated, rest withheld
    recreated = [p for p in bs_server.store.pages if p["name"] == "Page 0"]
    assert len(recreated) == 1  # recreated remotely, under a new id
    assert recreated[0]["id"] != pages[0]["id"]
    for pg in pages[1:]:
        assert bs_server.store.page(pg["id"]) is None  # still gone remotely — withheld, no undo


def test_force_local_recreate_of_a_moved_and_edited_page_drops_the_old_index_entry(
    run_grison, bs_server
):
    """A MOVE_EDIT (renamed AND edited locally) whose remote record vanishes
    between classification and the pre-write re-fetch, pushed with
    ``--force-local``: the recreate branch re-points the NEW path at the new id
    and must also drop the OLD path's entry, which still named the dead id.
    Before the fix the old entry lingered, so the next sync reported a phantom
    record at the old path and forgot it one cycle late."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="Notes body.")
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "field-notes.md"
    old_path.rename(new_path)
    new_path.write_text(
        new_path.read_text(encoding="utf-8").replace("Notes body.", "Notes body, edited."),
        encoding="utf-8",
    )

    def delete_on_bookstack() -> None:
        bs_server.store.pages.remove(bs_server.store.page(page["id"]))  # gone, not recycled

    # GET 1 is the identity-pairing re-fetch (the page still exists, so the rename
    # pairs as a MOVE_EDIT); the page vanishes right before GET 2, the pre-write
    # re-fetch, which is what sends apply down the "re-create remotely" branch.
    bs_server.on_request("GET", r"/api/pages/\d+", delete_on_bookstack, call_number=2)

    result = run_grison("sync", "--force-local", str(new_path))

    assert "re-created remotely" in result.output
    index = Index.load(Path.cwd())
    assert index.get("methodology/library/playbook/notes.md") is None
    new_rec = index.get("methodology/library/playbook/field-notes.md")
    assert new_rec is not None and new_rec.id != page["id"]
    assert "Notes body, edited." in bs_server.store.page(new_rec.id)["markdown"]

    second = run_grison("sync")
    assert "wiki (bs.page): clean 1" in second.output
    assert "forget" not in second.output


def test_wysiwyg_page_is_skipped_not_mirrored(run_grison, bs_server):
    """A wysiwyg page grison has never managed is an INFO-severity veto (nothing
    lost, nothing to do) — hidden from plain text output by default (tests changed
    on purpose: item 4's veto-severity split; ``--verbose`` shows it, ``--json``
    always would)."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(
        book_id=book["id"],
        name="WYSIWYG Page",
        editor="wysiwyg",
        markdown="",
        raw_html="<p>Authored in the rich editor.</p>",
    )

    quiet = run_grison("sync")
    assert "wysiwyg" not in quiet.output.lower()  # INFO severity: hidden by default

    lib = Path.cwd() / "methodology" / "library"
    assert not list(lib.rglob("wysiwyg-page.md"))

    verbose = run_grison("sync", "--verbose")
    assert 'skip "WYSIWYG Page" (page' in verbose.output  # identifies its record (item 2)


def test_draft_page_is_skipped(run_grison, bs_server):
    """A draft is INFO-severity — same rationale as the wysiwyg case above."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Draft Page", markdown="# D", draft=True)

    quiet = run_grison("sync")
    assert "draft" not in quiet.output.lower()

    lib = Path.cwd() / "methodology" / "library"
    assert not list(lib.rglob("draft-page.md"))

    verbose = run_grison("sync", "--verbose")
    assert "draft" in verbose.output.lower()
    assert 'skip "Draft Page" (page' in verbose.output


# --- structure: chapter/book moves, renames, chapter creation -------------------


def test_page_moved_to_another_chapter_locally_pushes(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap_a = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    chap_b = bs_server.store.seed_chapter(book_id=book["id"], name="Exploitation")
    page = bs_server.store.seed_page(
        book_id=book["id"],
        chapter_id=chap_a["id"],
        name="Notes",
        markdown="Notes body.",
    )
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "exploitation" / "notes.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)

    result = run_grison("sync")

    assert "move" in result.output
    assert bs_server.store.page(page["id"])["chapter_id"] == chap_b["id"]
    assert (
        Index.load(Path.cwd()).get("methodology/library/playbook/exploitation/notes.md").id
        == page["id"]
    )


def test_page_moved_between_chapters_remotely_relocates_local_file(run_grison, bs_server):
    """tests changed on purpose: a remote-side parent change now relocates the local
    file automatically on pull (the PULL-side counterpart of a local move) — there is
    no more 'structure-drift, converges only once the user moves the file by hand'."""
    book = bs_server.store.seed_book(name="Playbook")
    chap_a = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    chap_b = bs_server.store.seed_chapter(book_id=book["id"], name="Exploitation")
    page = bs_server.store.seed_page(
        book_id=book["id"],
        chapter_id=chap_a["id"],
        name="Notes",
        markdown="# Notes",
    )
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "exploitation" / "notes.md"
    bs_server.store.edit_page(page["id"], chapter_id=chap_b["id"])

    result = run_grison("sync")

    assert "pull" in result.output
    assert not old_path.exists()
    assert new_path.exists()
    assert (
        Index.load(Path.cwd()).get("methodology/library/playbook/exploitation/notes.md").id
        == page["id"]
    )


def test_remote_book_move_relocates_the_local_file(run_grison, bs_server):
    """tests changed on purpose: replaces the old 'book-rename tripwire'/'structure-
    drift' pair — a remote book move is just another relocation on pull, same as a
    chapter move; there is no more drift state or book-rename block."""
    book_a = bs_server.store.seed_book(name="Playbook")
    book_b = bs_server.store.seed_book(name="Web")
    page = bs_server.store.seed_page(book_id=book_a["id"], name="Notes", markdown="# N")
    run_grison("sync")
    bs_server.store.edit_page(page["id"], book_id=book_b["id"], chapter_id=0)

    result = run_grison("sync")

    assert "pull" in result.output
    assert not (Path.cwd() / "methodology/library/playbook/notes.md").exists()
    assert (Path.cwd() / "methodology/library/web/notes.md").exists()


def test_new_page_in_an_existing_chapter_dir_creates(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    run_grison("sync")  # materializes playbook/recon/
    newp = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "new-notes.md"
    _write_page_file(newp, title="New Notes", body="New notes body text.")

    result = run_grison("sync")

    assert "wiki (bs.page): create 1" in result.output
    created = [p for p in bs_server.store.pages if p["chapter_id"] == chap["id"]]
    assert len(created) == 1


def test_new_local_book_directory_creates_the_book_on_bookstack(run_grison, bs_server):
    newp = Path.cwd() / "methodology" / "library" / "new-book" / "first-page.md"
    _write_page_file(newp, title="First Page", body="First page body text.")

    result = run_grison("sync")

    assert "create book new-book" in result.output
    book = next(b for b in bs_server.store.books if b["slug"] == "new-book")
    created = [p for p in bs_server.store.pages if p["book_id"] == book["id"]]
    assert len(created) == 1
    assert created[0]["name"] == "First Page"


def test_new_local_chapter_directory_creates_the_chapter_on_bookstack(run_grison, bs_server):
    bs_server.store.seed_book(name="Playbook")
    run_grison("sync")
    newp = Path.cwd() / "methodology" / "library" / "playbook" / "new-chapter" / "first-page.md"
    _write_page_file(newp, title="First Page", body="First page body text.")

    result = run_grison("sync")

    assert "create chapter methodology/library/playbook/new-chapter" in result.output
    book = next(b for b in bs_server.store.books if b["slug"] == "playbook")
    chapter = next(c for c in bs_server.store.chapters if c["slug"] == "new-chapter")
    assert chapter["book_id"] == book["id"]
    created = [p for p in bs_server.store.pages if p["chapter_id"] == chapter["id"]]
    assert len(created) == 1


# --- priority / tags / title, both directions ------------------------------------


def test_priority_pulls_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N", priority=1)
    run_grison("sync")
    bs_server.store.edit_page(page["id"], priority=7)

    result = run_grison("sync")

    assert "wiki (bs.page): pull 1" in result.output
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    assert fm["priority"] == 7


def test_priority_pushes_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N", priority=1)
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N", priority=9)

    result = run_grison("sync")

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["priority"] == 9


def test_valued_tags_pull_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    bs_server.store.edit_page(page["id"], tags=[{"name": "owasp", "value": "A01"}])
    run_grison("sync")

    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    # v2 tags are plain strings (no name/value structure in the frontmatter model) —
    # a valued BookStack tag round-trips as "name:value" (bs_pages.py's own
    # documented convention).
    assert fm["tags"] == ["owasp:A01"]


def test_valued_tags_push_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N", tags=["owasp:A02"])

    result = run_grison("sync")

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["tags"] == [{"name": "owasp", "value": "A02"}]


def test_title_change_pulls_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    bs_server.store.edit_page(page["id"], name="Renamed On BookStack")

    result = run_grison("sync")

    assert "wiki (bs.page): pull 1" in result.output
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    assert fm["title"] == "Renamed On BookStack"
    # D4/the brief's "names are stable handles": a title change never renames the file
    assert path.exists()


def test_title_change_pushes_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Renamed By Hand", body="# N")

    result = run_grison("sync")

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["name"] == "Renamed By Hand"
    assert path.exists()  # the file itself never gets renamed


def test_renaming_the_local_file_is_a_pure_move_no_write(run_grison, bs_server):
    """A file rename with unchanged content is an index-only bookkeeping move — no
    remote write at all (ENGINE.md 'MOVE': never a no-op PUT)."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    new_path = old_path.parent / "renamed-file.md"
    old_path.rename(new_path)

    result = run_grison("sync")

    assert "move" in result.output
    assert bs_server.operation_log == []  # pure rename: zero BookStack writes
    assert new_path.exists() and not old_path.exists()


# --- remote-gone / recycle-bin / corrupt-file ------------------------------------


def test_remote_delete_lands_in_the_recycle_bin_and_is_skipped_not_deleted(run_grison, bs_server):
    """tests changed on purpose (BRIEF B, recycle-bin awareness): a page missing
    from the live lists but present in /api/recycle-bin is a SKIP, recoverable —
    never treated as gone (the old module's 'orphan skip' had the right verb but the
    wrong reason; the new engine's delete_local path, proven above, is what a
    TRULY-gone page gets instead)."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    from grison.remote.bookstack import BookStackClient
    from grison.remote.creds import Creds

    BookStackClient(
        Creds(
            bs_url="https://x",
            bs_token_id=bs_server.token_id,
            bs_token_secret=bs_server.token_secret,
        ),
        transport=bs_server.transport,
    ).delete_page(page["id"])

    result = run_grison("sync")

    assert "recycle bin" in result.output
    assert bs_server.store.page(page["id"]) is None
    assert any(d["deletable_id"] == page["id"] for d in bs_server.store.recycle_bin)
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    assert path.exists()  # the local file is left alone, not deleted

    # the record stays SKIPped, quietly, on every later sync too — never re-surfaces
    # as anything else and never disappears from the recycle-bin awareness either
    result2 = run_grison("sync")
    assert "recycle bin" in result2.output


def test_unknown_recycled_page_is_ignored_silently(run_grison, bs_server):
    """Coordinator feedback item 3: a page grison never indexed that happens to sit
    in the recycle bin (someone else's housekeeping, or one this workspace never
    pulled) is none of grison's business — no event, not counted, not on every sync
    forever. Only an INDEXED record whose remote landed in the bin gets that SKIP
    (proven above)."""
    book = bs_server.store.seed_book(name="Playbook")
    tracked = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")  # indexes "Notes" only

    # a page grison never even pulled, deleted directly on the server — never indexed
    untracked = bs_server.store.seed_page(book_id=book["id"], name="Untracked", markdown="# U")
    from grison.remote.bookstack import BookStackClient
    from grison.remote.creds import Creds

    BookStackClient(
        Creds(
            bs_url="https://x",
            bs_token_id=bs_server.token_id,
            bs_token_secret=bs_server.token_secret,
        ),
        transport=bs_server.transport,
    ).delete_page(untracked["id"])
    assert any(d["deletable_id"] == untracked["id"] for d in bs_server.store.recycle_bin)

    result = run_grison("sync", "--verbose")  # --verbose too: still nothing about it

    assert "Untracked" not in result.output
    assert "wiki (bs.page): clean 1" in result.output  # only "Notes" counted, nothing else
    del tracked


def test_corrupt_local_page_file_is_an_error_isolated_from_others(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    good = bs_server.store.seed_page(book_id=book["id"], name="Good", markdown="# Good")
    run_grison("sync")
    good_path = Path.cwd() / "methodology" / "library" / "playbook" / "good.md"
    _write_page_file(good_path, title="Good", body="Good body.\n\nEdited.")
    broken_path = Path.cwd() / "methodology" / "library" / "playbook" / "broken.md"
    broken_path.write_text("no frontmatter fence at all", encoding="utf-8")

    result = run_grison("sync")

    assert "broken.md" in result.output
    assert bs_server.store.page(good["id"])["markdown"] == "Good body.\n\nEdited."


def test_too_deeply_nested_local_file_is_never_scanned_but_validate_flags_it(
    run_grison,
    bs_server,
):
    """tests changed on purpose: BookStack has no deeper nesting than book/chapter/
    page, so the adapter simply never looks past chapter depth (not a sync-time
    trip-wire message any more) — the file's presence under an extra directory is a
    validator failure (WS-003) instead."""
    bs_server.store.seed_book(name="Playbook")
    deep = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "extra" / "x.md"
    _write_page_file(deep, title="X", body="x")

    result = run_grison("sync")

    assert bs_server.store.pages == []  # never scanned, never created

    validate_result = run_grison("validate")
    assert "WS-003" in validate_result.output
    del result


def test_remote_relocation_onto_an_occupied_local_path_is_tripwired(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    # will try to relocate here
    bs_server.store.edit_page(page["id"], chapter_id=chap["id"])
    blocker = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("unrelated local file already here", encoding="utf-8")

    result = run_grison("sync")

    assert "failed" in result.output.lower()
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    assert old_path.exists()
    assert blocker.read_text(encoding="utf-8") == "unrelated local file already here"


# --- structure mirrors: content, regeneration, hand-edit preservation -----------


def test_book_and_chapter_mirror_content_after_first_sync(run_grison, bs_server):
    book = bs_server.store.seed_book(
        name="Playbook",
        description="Playbook description",
        tags=[{"name": "topic", "value": "network"}],
    )
    chap = bs_server.store.seed_chapter(
        book_id=book["id"], name="Recon", priority=2, description="Recon phase"
    )
    run_grison("sync")

    book_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml").read_text()
    )
    assert book_mirror["name"] == "Playbook"
    assert book_mirror["slug"] == "playbook"
    assert book_mirror["description"] == "Playbook description"
    assert book_mirror["tags"] == ["topic:network"]

    chap_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / "playbook" / "recon" / ".chapter.yml").read_text()
    )
    assert chap_mirror["name"] == "Recon"
    assert chap_mirror["priority"] == 2
    assert chap_mirror["description"] == "Recon phase"
    assert _indexed_id(Path.cwd(), "methodology/library/playbook") == book["id"]
    assert _indexed_id(Path.cwd(), "methodology/library/playbook/recon") == chap["id"]


def test_mirror_regenerates_after_remote_structure_change(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook", description="old description")
    run_grison("sync")
    mirror = Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml"
    book["description"] = "new description"
    book["updated_at"] = "2099-01-01T00:00:00.000000Z"  # a real edit bumps this too

    run_grison("sync")

    assert "new description" in mirror.read_text(encoding="utf-8")
    assert not mirror.with_suffix(".remote.yml").exists()


def test_mirror_hand_edit_is_preserved_and_flagged_invalid(run_grison, bs_server):
    """tests changed on purpose (D6/ENGINE.md read-only record types): a hand-edited
    mirror is now a `grison validate` failure (WS-009), never silently overwritten
    and never sidecar'ed — 'read-only types take PULL only when locally unedited'."""
    bs_server.store.seed_book(name="Playbook", description="original")
    run_grison("sync")
    mirror = Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml"
    edited = mirror.read_text(encoding="utf-8").replace("original", "hand-edited by user")
    mirror.write_text(edited, encoding="utf-8")

    run_grison("sync")  # remote unchanged

    assert mirror.read_text(encoding="utf-8") == edited  # never silently overwritten
    assert not mirror.with_suffix(".remote.yml").exists()  # no sidecar mechanism for mirrors

    validate_result = run_grison("validate")
    assert "WS-009" in validate_result.output
    assert "git checkout" in validate_result.output


# --- concurrent-drift guards, using the fake's request hooks --------------------


def test_wysiwyg_guard_is_rechecked_immediately_before_the_push(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited by human")

    def flip_to_wysiwyg() -> None:
        row = bs_server.store.page(page["id"])
        row["editor"] = "wysiwyg"
        row["raw_html"] = "<p>rich content authored on BookStack</p>"

    # tests changed on purpose: with the skip-detail-fetch fast path, classify never
    # issues its own detail GET when the witness is unchanged (proven separately by
    # test_second_sync_skips_page_detail_fetch_for_clean_pages) — the ONE real detail
    # GET this run makes is the pre-write re-fetch guard itself, call_number=1.
    bs_server.on_request("GET", r"/api/pages/\d+", flip_to_wysiwyg, call_number=1)

    result = run_grison("sync")

    assert "wysiwyg" in result.output.lower()
    # never overwritten with the pending local edit — the guard refused the push
    assert bs_server.store.page(page["id"])["markdown"] == "# Original"
    assert "Edited by human" in path.read_text(encoding="utf-8")  # local edit preserved


def test_second_sync_skips_page_detail_fetch_for_clean_pages(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    before = len(bs_server.request_log)

    run_grison("sync")

    new_requests = bs_server.request_log[before:]
    detail_gets = [
        r
        for r in new_requests
        if r.method == "GET" and r.path.startswith("/api/pages/") and r.path != "/api/pages"
    ]
    assert detail_gets == []  # the skip-detail-fetch fast path really skipped the GET


def test_remote_drift_during_push_is_a_collision_not_an_overwrite(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited by human")

    def rename_on_bookstack() -> None:
        bs_server.store.page(page["id"])["name"] = "Renamed Concurrently"

    # tests changed on purpose: see test_wysiwyg_guard_is_rechecked's own note —
    # the skip-detail-fetch fast path means the pre-write re-fetch is the only
    # detail GET this run makes.
    bs_server.on_request("GET", r"/api/pages/\d+", rename_on_bookstack, call_number=1)

    result = run_grison("sync")

    assert "collision" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Original"  # push refused entirely
    assert bs_server.store.page(page["id"])["name"] == "Renamed Concurrently"  # preserved
    assert "Edited by human" in path.read_text(encoding="utf-8")  # local edit not lost


def test_force_local_bypasses_the_concurrent_drift_guard(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited by human")

    def rename_on_bookstack() -> None:
        bs_server.store.page(page["id"])["name"] = "Renamed Concurrently"

    bs_server.on_request("GET", r"/api/pages/\d+", rename_on_bookstack, call_number=2)

    result = run_grison("sync", "--force-local", str(path))

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited by human"


# --- closing gaps vs. tests/test_methodology.py / test_methodology_guards.py -----


def test_local_move_to_a_different_book_pushes_with_the_new_book_id(run_grison, bs_server):
    book_a = bs_server.store.seed_book(name="Playbook")
    book_b = bs_server.store.seed_book(name="Web")
    page = bs_server.store.seed_page(book_id=book_a["id"], name="Notes", markdown="# N")
    other = bs_server.store.seed_page(book_id=book_a["id"], name="Other", markdown="# O")
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "web" / "notes.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)

    result = run_grison("sync")

    assert "move" in result.output
    assert bs_server.store.page(page["id"])["book_id"] == book_b["id"]
    assert bs_server.store.page(other["id"])["book_id"] == book_a["id"]  # sibling untouched


def test_shelf_mirror_content_and_book_membership(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_shelf(
        name="Engagements", description="Client engagement books", book_ids=[book["id"]]
    )

    run_grison("sync")

    shelf_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / ".shelves" / "engagements.yml").read_text()
    )
    assert shelf_mirror["description"] == "Client engagement books"
    assert shelf_mirror["books"] == ["playbook"]
    book_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml").read_text()
    )
    assert book_mirror["shelves"] == ["engagements"]


def test_checklists_directory_is_never_synced(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    checklist = Path.cwd() / "methodology" / "checklists" / "acme-engagement" / "notes.md"
    checklist.parent.mkdir(parents=True, exist_ok=True)
    _write_page_file(checklist, title="Notes", body="engagement-local working copy")

    run_grison("sync")

    assert "engagement-local working copy" in checklist.read_text(encoding="utf-8")
    assert len(bs_server.store.pages) == 1  # the checklist copy never became a remote page


def test_bare_content_push_does_not_eject_a_chaptered_page(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"],
        chapter_id=chap["id"],
        name="Notes",
        markdown="# N",
    )
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nMore detail.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "wiki (bs.page): push 1" in result.output
    assert bs_server.store.page(page["id"])["chapter_id"] == chap["id"]  # still in its chapter


def test_mirror_first_materialization_has_the_generated_header(run_grison, bs_server):
    bs_server.store.seed_book(name="Playbook", description="d")

    run_grison("sync")

    text = (Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml").read_text(
        encoding="utf-8"
    )
    assert text.startswith("# generated by grison")


def test_undo_restores_a_deleted_page(run_grison, bs_server):
    """Proves a real bug found via the lab proof: DELETE_REMOTE's forward direction
    never touches the local file (it was already missing — that's what made it
    DELETE_REMOTE), so undoing it must restore the local mirror too, or the very
    next ordinary sync would see "missing, indexed" again and immediately re-delete
    (or collide on) the record undo just brought back."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    path.unlink()
    run_grison("sync")  # deletes it remotely (and snapshots the pre-image)
    assert bs_server.store.page(page["id"]) is None
    assert not path.exists()

    result = run_grison("undo")

    assert result.exit_code == 0, result.output
    restored = [p for p in bs_server.store.pages if p["name"] == "Notes"]
    assert len(restored) == 1
    assert restored[0]["markdown"] == "# N"
    assert path.exists()  # the local mirror comes back too, not just the remote record
    assert path.read_text(encoding="utf-8").strip().endswith("# N")

    # the very next ordinary sync must stay clean — no re-delete, no collision
    result2 = run_grison("sync")
    assert "wiki (bs.page): clean 1" in result2.output
    assert bs_server.store.page(restored[0]["id"]) is not None


def test_undo_reverts_a_push(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited")
    run_grison("sync")
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited"

    result = run_grison("undo")

    assert result.exit_code == 0, result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Original"


def test_undo_exits_2_when_credentials_are_missing(run_grison, workspace, bs_server):
    """Item 6 (MEDIUM, fix-fin1): ``grison undo``'s own ``creds.require_bookstack()``
    call has no local try/except — before this fix it fell through to ``_guarded``'s
    generic ``GrisonError`` handling and exited 1. ENGINE.md §10 puts missing/bad
    credentials in the "could not run" class (exit 2), same as no-workspace/
    incompatible-server/lock-held — never the "ran but needs attention" class."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited")
    run_grison("sync")  # a real push -> a bs.page-kind snapshot exists to undo
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited"

    # BookStack credentials go missing between the push and the undo attempt.
    env_path = workspace / ".grison" / "env"
    text = env_path.read_text(encoding="utf-8")
    text = re.sub(r"^GRISON_BS_TOKEN_ID=.*$", "GRISON_BS_TOKEN_ID=", text, flags=re.MULTILINE)
    text = re.sub(
        r"^GRISON_BS_TOKEN_SECRET=.*$", "GRISON_BS_TOKEN_SECRET=", text, flags=re.MULTILINE
    )
    env_path.write_text(text, encoding="utf-8")

    result = run_grison("undo")

    assert result.exit_code == 2, result.output
    assert "missing BookStack credentials" in result.output
    # nothing was undone — the push survives exactly as it was.
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited"


def test_undo_of_a_push_refuses_to_clobber_a_record_edited_again_since(run_grison, bs_server):
    """Item 4 (fix-d): before this fix, ``grison undo``'s push/move_edit replay
    branch called ``adapter.restore`` straight over ``remote_preimage`` with no
    re-fetch guard at all — unlike the create/delete_remote branches, contradicting
    the ``grison.engine.undo`` module docstring's own claim that every replayed op
    is "guarded by the same re-fetch check the forward apply loop uses". A record
    edited again (on the server, by someone else) after the push this undo would
    reverse must be reported, not silently overwritten with the older content."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# Edited")
    run_grison("sync")
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited"

    # someone else edits the SAME record on the server after grison's push, before
    # anyone runs `grison undo`.
    bs_server.store.edit_page(page["id"], markdown="# Edited further, by someone else")

    result = run_grison("undo")

    assert result.exit_code == 1, result.output
    assert "methodology/library/playbook/notes.md" in result.output
    # never clobbered — the concurrent edit survives exactly as it was
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited further, by someone else"


def test_undo_list_shows_snapshots_newest_first(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")  # pull-only — no snapshot at all (see the read-only test below)
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N\n\nedit 1")
    run_grison("sync")  # push #1 — one real snapshot
    _write_page_file(path, title="Notes", body="# N\n\nedit 2")
    run_grison("sync")  # push #2 — a second, newer snapshot

    result = run_grison("undo", "--list")

    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 2  # exactly the two push runs — the pull-only run left none
    assert lines == sorted(lines, reverse=True)


def test_undo_list_shows_time_and_verb_counts_per_snapshot(run_grison, bs_server):
    """``grison undo --list`` shows per snapshot: time, and counts per verb —
    coordinator feedback item 1, e.g. '2026-09-17 17:32  push 1'."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N\n\nedited")

    result = run_grison("undo", "--list")

    assert result.output.strip() == "no snapshots"  # no push happened yet — nothing to list

    run_grison("sync")  # now push the edit
    result2 = run_grison("undo", "--list")
    lines2 = [ln for ln in result2.output.splitlines() if ln.strip()]
    assert len(lines2) == 1
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}  push 1$", lines2[0]), lines2[0]


def test_undo_prints_what_it_did_per_record(run_grison, bs_server):
    """``grison undo`` prints what it did per record — coordinator feedback item 1."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N\n\nedited")
    run_grison("sync")

    result = run_grison("undo")

    assert "methodology/library/playbook/notes.md" in result.output
    assert "restored pre-undo content" in result.output
    del page


def test_read_only_sync_leaves_no_snapshot(run_grison, bs_server):
    """Coordinator feedback item 1: a run with no remote write leaves no snapshot at
    all — undo is for remote writes; with prune-to-10, ordinary pull-only syncs would
    otherwise evict every real undo point."""
    book = bs_server.store.seed_book(name="Playbook")
    for i in range(3):
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"Body {i}.")

    result = run_grison("sync")  # pure pull_new — no remote write at all

    assert "snapshot:" not in result.output
    assert run_grison("undo", "--list").output.strip() == "no snapshots"


def test_second_read_only_sync_does_not_evict_the_one_real_snapshot(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, title="Notes", body="# N\n\nedited")
    run_grison("sync")  # the one real (push) snapshot

    for _ in range(3):
        run_grison("sync")  # clean, read-only — must not touch the snapshot history

    assert len(run_grison("undo", "--list").output.strip().splitlines()) == 1
    del page
