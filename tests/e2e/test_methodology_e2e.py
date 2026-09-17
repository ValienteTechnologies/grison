"""End-to-end methodology (BookStack) sync scenarios — CLI + on-disk files +
``bs_server``'s inspection API only. No import from ``grison.remote.methodology``/
``state``, no assertion on private state-file contents.

Every ``run_grison("sync")`` here also runs the findings phase, which today fails
unconditionally (see ``tests/e2e/test_findings_e2e.py`` — ``GhostwriterClient
.fetch_evidence`` selects a field the real 7.2.6 schema rejects, regardless of
data). That failure is isolated per phase (``grison.cli._run_phase``) and always
makes the *overall* process exit 1 and print "findings sync failed: …", even when
methodology itself is perfectly clean — so these tests assert methodology's own
success signals (its own summary line, the BookStack operation log, the files on
disk) rather than the overall exit code. This is today's real, documented
cross-phase coupling, not a gap in the fakes.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _read_fm(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return fm, body


def _write_page_file(
    path: Path, *, page_id: int | None, title: str, book: str, body: str,
    chapter: str | None = None, priority: int | None = None, tags: list | None = None,
) -> None:
    fm: dict = {"grison": {"kind": "methodology", "bs": {}}}
    if page_id is not None:
        fm["grison"]["bs"]["page_id"] = page_id
    fm["title"] = title
    fm["book"] = book
    if chapter:
        fm["chapter"] = chapter
    if priority is not None:
        fm["priority"] = priority
    if tags:
        fm["tags"] = tags
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False).strip() + f"\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _touch_remote_page(bs_server, page_id: int, **fields) -> None:
    """Simulate an out-of-band edit made directly on BookStack (e.g. through the web
    UI) — bumps ``updated_at``/``revision_count`` like a real ``update_page`` would,
    so grison's skip-detail-fetch fast path (keyed on those markers) doesn't mistake
    the change for "nothing happened"."""
    page = bs_server.store.page(page_id)
    page.update(fields)
    page["revision_count"] += 1
    page["updated_at"] = "2099-01-01T00:00:00.000000Z"


def test_first_sync_pulls_everything(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chapter = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chapter["id"], name="Getting Started",
        markdown="# Getting Started\n\nHello.",
    )

    result = run_grison("sync")

    assert "methodology: pull 1, push 0, create 0  (0 clean, 0 repaired)" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/recon/getting-started.md"
    assert page_path.exists()
    fm, body = _read_fm(page_path)
    assert fm["grison"]["bs"]["page_id"] == page["id"]
    assert fm["title"] == "Getting Started"
    assert fm["book"] == "playbook"
    assert fm["chapter"] == "recon"
    assert body == "# Getting Started\n\nHello."
    assert (page_path.parent.parent / ".book.yml").exists()
    assert (page_path.parent / ".chapter.yml").exists()
    assert bs_server.operation_log == []  # pull never mutates BookStack


def test_second_sync_is_a_noop(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    before = page_path.read_text(encoding="utf-8")
    before_mtime = page_path.stat().st_mtime_ns

    result = run_grison("sync")

    assert "methodology: pull 0, push 0, create 0  (1 clean, 0 repaired)" in result.output
    assert page_path.read_text(encoding="utf-8") == before
    assert page_path.stat().st_mtime_ns == before_mtime
    assert bs_server.operation_log == []  # zero mutations on the fake, both syncs


def test_local_edit_pushes(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    fm, _ = _read_fm(page_path)
    _write_page_file(page_path, page_id=page["id"], title="Getting Started", book="playbook",
                      body="# Hi\n\nEdited by hand.")

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Hi\n\nEdited by hand."
    names = [o.name for o in bs_server.operation_log]
    assert names == ["update_page"]


def test_remote_edit_pulls(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    _touch_remote_page(bs_server, page["id"], markdown="# Hi\n\nChanged on BookStack.")

    result = run_grison("sync")

    assert "methodology: pull 1, push 0, create 0" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nChanged on BookStack."
    assert bs_server.operation_log == []  # a pull never writes to BookStack


def test_collision_surfaced_never_overwritten_then_force_flags(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, page_id=page["id"], title="Getting Started", book="playbook",
                      body="# Hi\n\nLocal change.")
    _touch_remote_page(bs_server, page["id"], markdown="# Hi\n\nRemote change.")

    result = run_grison("sync")

    assert "collision" in result.output
    # local is untouched; the remote side is surfaced in a sidecar, never overwritten
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nLocal change."
    sidecar = page_path.with_suffix(".remote.md")
    assert sidecar.exists()
    assert "Remote change." in sidecar.read_text(encoding="utf-8")

    result_force_local = run_grison("sync", "--force-local", str(page_path))
    assert "methodology: pull 0, push 1, create 0" in result_force_local.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Hi\n\nLocal change."


def test_force_remote_resolves_collision(run_grison, bs_server):
    """Current behavior — NOT yet what D6 promises ("one collision-sidecar policy,
    stale sidecars cleared everywhere"): unlike the findings phase (sync.py's
    ``_clear_sidecar``, called after every forced push/pull), methodology's ``_apply``
    "pull" branch never removes the ``.remote.md`` sidecar. ``--force-remote`` still
    correctly resolves the collision on the tracked file — the stale sidecar left
    behind is the documented gap the engine rewrite (D6) is meant to close."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, page_id=page["id"], title="Getting Started", book="playbook",
                      body="# Hi\n\nLocal change.")
    _touch_remote_page(bs_server, page["id"], markdown="# Hi\n\nRemote change.")
    run_grison("sync")  # first surfaces the collision + sidecar

    result = run_grison("sync", "--force-remote", str(page_path))

    assert "methodology: pull 1, push 0, create 0" in result.output
    _, body = _read_fm(page_path)
    assert body == "# Hi\n\nRemote change."  # the tracked file itself IS resolved
    assert page_path.with_suffix(".remote.md").exists()  # but the sidecar is left stale (see above)


def test_new_local_page_creates(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page_path = Path.cwd() / "methodology" / "library" / "playbook" / "new-page.md"
    _write_page_file(page_path, page_id=None, title="New Page", book="playbook", body="# New Page")

    result = run_grison("sync")

    assert "methodology: pull 0, push 0, create 1" in result.output
    fm, body = _read_fm(page_path)
    assert isinstance(fm["grison"]["bs"]["page_id"], int)
    assert body == "# New Page"
    created = [p for p in bs_server.store.pages if p["book_id"] == book["id"]]
    assert len(created) == 1
    assert created[0]["markdown"] == "# New Page"


def test_local_delete_is_repulled_not_deleted_remotely(run_grison, bs_server):
    """Current behavior, not (yet) a delete-sync feature: deleting a tracked local
    file does not delete the remote page — the next sync just re-creates the local
    file from the still-live remote record (the identity/base lives in
    ``.grison/state``, keyed by page_id, which the deleted file never touched)."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    page_path.unlink()

    result = run_grison("sync")

    assert "methodology: pull 1, push 0, create 0" in result.output
    assert page_path.exists()
    assert bs_server.operation_log == []  # nothing was ever deleted on BookStack


def test_remote_delete_is_an_orphan_skip(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    bs_server.store.pages.remove(bs_server.store.page(page["id"]))

    result = run_grison("sync")

    assert "remote page gone (orphan)" in result.output
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    assert page_path.exists()  # the local file is left alone, not deleted


def test_dry_run_writes_nothing(run_grison, bs_server, tmp_path):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("sync", "--dry-run")

    assert "would pull" in result.output.lower() or "pull 1" in result.output
    lib = Path.cwd() / "methodology" / "library"
    assert not any(lib.rglob("*.md"))  # nothing landed on disk
    assert bs_server.operation_log == []


def test_malformed_local_file_does_not_stop_others(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    good = bs_server.store.seed_page(book_id=book["id"], name="Good Page", markdown="# Good")
    run_grison("sync")
    good_path = Path.cwd() / "methodology/library/playbook/good-page.md"
    _write_page_file(good_path, page_id=good["id"], title="Good Page", book="playbook",
                      body="# Good\n\nEdited.")
    broken_path = Path.cwd() / "methodology" / "library" / "playbook" / "broken.md"
    broken_path.write_text("not frontmatter at all", encoding="utf-8")

    result = run_grison("sync")

    assert bs_server.store.page(good["id"])["markdown"] == "# Good\n\nEdited."
    assert str(broken_path) in result.output or "broken.md" in result.output


def test_server_failure_on_one_page_does_not_stop_the_other(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    p1 = bs_server.store.seed_page(book_id=book["id"], name="Page One", markdown="# One")
    p2 = bs_server.store.seed_page(book_id=book["id"], name="Page Two", markdown="# Two")
    run_grison("sync")
    path1 = Path.cwd() / "methodology/library/playbook/page-one.md"
    path2 = Path.cwd() / "methodology/library/playbook/page-two.md"
    _write_page_file(path1, page_id=p1["id"], title="Page One", book="playbook",
                      body="# One\n\nEdited.")
    _write_page_file(path2, page_id=p2["id"], title="Page Two", book="playbook",
                      body="# Two\n\nEdited.")
    bs_server.inject_http_500(times=1, method="PUT")  # only a page update fails, not the reads

    result = run_grison("sync")

    succeeded = [p for p in (p1, p2)
                 if bs_server.store.page(p["id"])["markdown"].endswith("Edited.")]
    assert len(succeeded) == 1  # exactly one push got through; the other was isolated
    assert "error" in result.output.lower()


def test_mass_change_guard_withholds_and_announces_it(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    pages = [
        bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"# {i}")
        for i in range(6)
    ]
    run_grison("sync")
    for i, p in enumerate(pages):
        path = Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md"
        _write_page_file(path, page_id=p["id"], title=f"Page {i}", book="playbook",
                          body=f"# {i}\n\nEdited.")

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on methodology — writes withheld." in result.output
    assert bs_server.operation_log == []  # every push was withheld, none reached BookStack


def test_wysiwyg_page_is_skipped_not_mirrored(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(
        book_id=book["id"], name="WYSIWYG Page", editor="wysiwyg", markdown="",
        raw_html="<p>Authored in the rich editor.</p>",
    )

    result = run_grison("sync")

    assert "wysiwyg page" in result.output
    lib = Path.cwd() / "methodology" / "library"
    assert not list(lib.rglob("wysiwyg-page.md"))


# --- structure: chapter/book moves, renames, chapter creation -------------------


def test_page_moved_to_another_chapter_locally_pushes(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap_a = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    chap_b = bs_server.store.seed_chapter(book_id=book["id"], name="Exploitation")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chap_a["id"], name="Notes", markdown="# Notes",
    )
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "exploitation" / "notes.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["chapter_id"] == chap_b["id"]


def test_page_moved_between_chapters_remotely_relocates_local_file(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap_a = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    chap_b = bs_server.store.seed_chapter(book_id=book["id"], name="Exploitation")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chap_a["id"], name="Notes", markdown="# Notes",
    )
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "exploitation" / "notes.md"
    _touch_remote_page(bs_server, page["id"], chapter_id=chap_b["id"])

    result = run_grison("sync")

    assert "move" in result.output
    assert not old_path.exists()
    assert new_path.exists()


def test_page_moved_to_book_root_locally_pushes(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chap["id"], name="Notes", markdown="# Notes",
    )
    run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    new_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    old_path.rename(new_path)

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["chapter_id"] == 0


def test_book_renamed_remotely_is_blocked_not_silently_reparented(run_grison, bs_server):
    """Current trip-wire (bs-structure F7): a remote book RENAME looks, from a single
    page's point of view, exactly like a page move onto a different, already-existing
    book directory once the old book's own directory is gone too — grison has no
    update_book call, so it refuses rather than silently reparenting onto whatever
    book now owns that slug."""
    book = bs_server.store.seed_book(name="Playbook")
    other = bs_server.store.seed_book(name="Unrelated")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Notes")
    run_grison("sync")
    import shutil

    old_dir = Path.cwd() / "methodology" / "library" / "playbook"
    new_dir = Path.cwd() / "methodology" / "library" / "unrelated"
    (old_dir / "notes.md").rename(new_dir / "notes.md")  # the one page moves into the other book
    shutil.rmtree(old_dir)  # ...and the old book's dir is genuinely gone (a real `mv`)

    result = run_grison("sync")

    assert "book rename" in result.output
    assert "playbook" in result.output and "unrelated" in result.output
    assert bs_server.store.page(bs_server.store.pages[0]["id"])["book_id"] == book["id"]
    del other  # only needed so "unrelated" already exists as a real, different book


def test_new_page_in_an_existing_chapter_dir_creates(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    run_grison("sync")  # materializes playbook/recon/
    newp = (
        Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "new-notes.md"
    )
    _write_page_file(newp, page_id=None, title="New Notes", book="playbook", chapter="recon",
                      body="# New Notes")

    result = run_grison("sync")

    assert "methodology: pull 0, push 0, create 1" in result.output
    created = [p for p in bs_server.store.pages if p["chapter_id"] == chap["id"]]
    assert len(created) == 1


def test_new_page_in_unknown_chapter_dir_errors_loudly(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    newp = (
        Path.cwd() / "methodology" / "library" / "playbook" / "no-such-chapter" / "new.md"
    )
    _write_page_file(newp, page_id=None, title="New", book="playbook", chapter="no-such-chapter",
                      body="# New")

    result = run_grison("sync")

    assert "unknown chapter 'no-such-chapter'" in result.output
    assert bs_server.store.pages == []
    del book


# --- priority / tags / title, both directions ------------------------------------


def test_priority_pulls_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N", priority=1)
    run_grison("sync")
    _touch_remote_page(bs_server, page["id"], priority=7)

    result = run_grison("sync")

    assert "methodology: pull 1, push 0, create 0" in result.output
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    assert fm["priority"] == 7


def test_priority_pushes_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N", priority=1)
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Notes", book="playbook", body="# N",
                      priority=9)

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["priority"] == 9


def test_valued_tags_pull_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    bs_server.store.page(page["id"])["tags"] = [{"name": "owasp", "value": "A01"}]
    run_grison("sync")

    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    assert fm["tags"] == [{"name": "owasp", "value": "A01"}]


def test_valued_tags_push_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Notes", book="playbook", body="# N",
                      tags=[{"name": "owasp", "value": "A02"}])

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["tags"] == [{"name": "owasp", "value": "A02"}]


def test_title_change_pulls_from_remote(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    _touch_remote_page(bs_server, page["id"], name="Renamed On BookStack")

    result = run_grison("sync")

    assert "methodology: pull 1, push 0, create 0" in result.output
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    fm, _ = _read_fm(path)
    assert fm["title"] == "Renamed On BookStack"


def test_title_change_pushes_from_local(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Renamed By Hand", book="playbook", body="# N")

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["name"] == "Renamed By Hand"


# --- remote-gone / duplicate-identity / corrupt-file -----------------------------


def test_remote_delete_lands_in_the_recycle_bin_and_is_an_orphan_skip(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    from grison.remote.bookstack import BookStackClient
    from grison.remote.creds import Creds

    BookStackClient(
        Creds(bs_url="https://x", bs_token_id=bs_server.token_id,
              bs_token_secret=bs_server.token_secret),
        transport=bs_server.transport,
    ).delete_page(page["id"])

    result = run_grison("sync")

    assert "remote page gone (orphan)" in result.output
    assert bs_server.store.page(page["id"]) is None
    assert any(d["deletable_id"] == page["id"] for d in bs_server.store.recycle_bin)
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    assert path.exists()  # the local file is left alone, not deleted


def test_duplicate_page_id_both_files_skipped_and_untouched(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    dup = path.parent / "notes-copy.md"
    dup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    result = run_grison("sync")

    assert "duplicate page_id" in result.output
    assert "methodology: pull 0, push 0, create 0" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# N"  # remote untouched


def test_corrupt_local_page_file_is_an_error_isolated_from_others(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    good = bs_server.store.seed_page(book_id=book["id"], name="Good", markdown="# Good")
    run_grison("sync")
    good_path = Path.cwd() / "methodology" / "library" / "playbook" / "good.md"
    _write_page_file(good_path, page_id=good["id"], title="Good", book="playbook",
                      body="# Good\n\nEdited.")
    broken_path = Path.cwd() / "methodology" / "library" / "playbook" / "broken.md"
    broken_path.write_text("no frontmatter fence at all", encoding="utf-8")

    result = run_grison("sync")

    assert "broken.md" in result.output
    assert bs_server.store.page(good["id"])["markdown"] == "# Good\n\nEdited."


# --- structure mirrors: content, regeneration, hand-edit preservation -----------


def test_book_and_chapter_mirror_content_after_first_sync(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook", description="Playbook description")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon", priority=2,
                                        description="Recon phase")
    run_grison("sync")

    book_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml").read_text()
    )
    assert book_mirror["grison"]["bs"]["book_id"] == book["id"]
    assert book_mirror["name"] == "Playbook"
    assert book_mirror["description"] == "Playbook description"

    chap_mirror = yaml.safe_load(
        (Path.cwd() / "methodology" / "library" / "playbook" / "recon" / ".chapter.yml")
        .read_text()
    )
    assert chap_mirror["grison"]["bs"] == {"chapter_id": chap["id"], "book_id": book["id"]}
    assert chap_mirror["name"] == "Recon"
    assert chap_mirror["priority"] == 2
    assert chap_mirror["description"] == "Recon phase"


def test_mirror_regenerates_after_remote_structure_change(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook", description="old description")
    run_grison("sync")
    mirror = Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml"
    book["description"] = "new description"  # changed directly on BookStack

    result = run_grison("sync")

    assert "new description" in mirror.read_text(encoding="utf-8")
    assert not mirror.with_suffix(".remote.yml").exists()
    del result


def test_mirror_hand_edit_is_preserved_with_sidecar_and_error(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook", description="original")
    run_grison("sync")
    mirror = Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml"
    edited = mirror.read_text(encoding="utf-8").replace("original", "hand-edited by user")
    mirror.write_text(edited, encoding="utf-8")

    result = run_grison("sync")  # remote unchanged

    assert mirror.read_text(encoding="utf-8") == edited  # never silently overwritten
    sidecar = mirror.with_suffix(".remote.yml")
    assert sidecar.exists()
    assert "original" in sidecar.read_text(encoding="utf-8")
    assert "read-only" in result.output
    del book


# --- artifact/corruption guardrail: blocks NEW, grandfathers PRE-EXISTING -------


def test_new_corruption_artifact_blocks_the_push(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Notes\n\nclean")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    corrupted = path.read_text(encoding="utf-8").replace(
        "clean", 'leaked <span class="citation-1">x</span>'
    )
    path.write_text(corrupted, encoding="utf-8")

    result = run_grison("sync")

    assert "artifact" in result.output.lower()
    assert "refused" in result.output.lower()
    assert bs_server.store.page(page["id"])["markdown"] == "# Notes\n\nclean"  # never pushed


def test_preexisting_corruption_artifact_is_grandfathered_not_blocking(run_grison, bs_server):
    """The artifact scan flags pre-existing corruption (surfaced in the output) but
    does not refuse an otherwise-unrelated edit — only a NEWLY introduced artifact
    blocks the push."""
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(
        book_id=book["id"], name="Notes",
        markdown='# Notes\n\nleaked <span class="citation-1">x</span> already here',
    )
    run_grison("sync")  # pulls the pre-existing corruption as-is, unvalidated
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    edited = path.read_text(encoding="utf-8") + "\n\nAn unrelated addition."
    path.write_text(edited, encoding="utf-8")

    result = run_grison("sync")

    assert "artifact" in result.output.lower()  # still surfaced...
    assert "methodology: pull 0, push 1, create 0" in result.output  # ...but not blocked
    assert "An unrelated addition." in bs_server.store.page(page["id"])["markdown"]


# --- concurrent-drift guards, using the fake's request hooks --------------------


def test_wysiwyg_guard_is_rechecked_immediately_before_the_push(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Notes", book="playbook",
                      body="# Edited by human")

    def flip_to_wysiwyg() -> None:
        row = bs_server.store.page(page["id"])
        row["editor"] = "wysiwyg"
        row["raw_html"] = "<p>rich content authored on BookStack</p>"

    bs_server.on_request("GET", r"/api/pages/\d+", flip_to_wysiwyg, call_number=2)

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
    detail_gets = [r for r in new_requests if r.method == "GET" and r.path.startswith(
        "/api/pages/") and r.path != "/api/pages"]
    assert detail_gets == []  # the skip-detail-fetch fast path really skipped the GET


def test_remote_drift_during_push_is_a_collision_not_an_overwrite(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Notes", book="playbook",
                      body="# Edited by human")

    def rename_on_bookstack() -> None:
        bs_server.store.page(page["id"])["name"] = "Renamed Concurrently"

    bs_server.on_request("GET", r"/api/pages/\d+", rename_on_bookstack, call_number=2)

    result = run_grison("sync")

    assert "collision" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Original"  # push refused entirely
    assert bs_server.store.page(page["id"])["name"] == "Renamed Concurrently"  # preserved
    assert "Edited by human" in path.read_text(encoding="utf-8")  # local edit not lost


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

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert not result.output.count("structure-drift")
    assert bs_server.store.page(page["id"])["book_id"] == book_b["id"]
    assert bs_server.store.page(other["id"])["book_id"] == book_a["id"]  # sibling untouched


def test_remote_book_move_drifts_then_follows_and_repairs(run_grison, bs_server):
    """Distinct from the book-RENAME tripwire (old dir gone): here both books still
    exist and only the ONE page moved remotely — drift, no writes, until the user
    moves the file to match; then it converges with no ping-pong."""
    book_a = bs_server.store.seed_book(name="Playbook")
    book_b = bs_server.store.seed_book(name="Web")
    page = bs_server.store.seed_page(book_id=book_a["id"], name="Notes", markdown="# N")
    run_grison("sync")
    _touch_remote_page(bs_server, page["id"], book_id=book_b["id"])  # moved on BookStack only

    r1 = run_grison("sync")
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    assert "structure-drift" in r1.output
    assert old_path.exists()  # nothing pulled/pushed while it's drifting

    new_path = Path.cwd() / "methodology" / "library" / "web" / "notes.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    r2 = run_grison("sync")
    assert "structure-drift" not in r2.output
    assert "methodology: pull 0, push 0, create 0" in r2.output  # a pure restamp, not a push

    r3 = run_grison("sync")  # converged — no ping-pong
    assert "structure-drift" not in r3.output
    assert bs_server.store.page(page["id"])["book_id"] == book_b["id"]


def test_shelf_mirror_content_and_book_membership(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_shelf(name="Engagements", description="Client engagement books",
                               book_ids=[book["id"]])

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
    checklist = (
        Path.cwd() / "methodology" / "checklists" / "acme-engagement" / "notes.md"
    )
    checklist.parent.mkdir(parents=True, exist_ok=True)
    _write_page_file(checklist, page_id=None, title="Notes", book="playbook",
                      body="engagement-local working copy")

    run_grison("sync")

    assert "engagement-local working copy" in checklist.read_text(encoding="utf-8")
    assert len(bs_server.store.pages) == 1  # the checklist copy never became a remote page


def test_bare_content_push_does_not_eject_a_chaptered_page(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(
        book_id=book["id"], chapter_id=chap["id"], name="Notes", markdown="# N",
    )
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n\nMore detail.", encoding="utf-8")

    result = run_grison("sync")

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["chapter_id"] == chap["id"]  # still in its chapter


def test_nested_too_deep_local_file_is_tripwired(run_grison, bs_server):
    bs_server.store.seed_book(name="Playbook")
    deep = (
        Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "extra" / "x.md"
    )
    _write_page_file(deep, page_id=None, title="X", book="playbook", body="x")

    result = run_grison("sync")

    assert "too deep" in result.output
    assert bs_server.store.pages == []


def test_pull_relocation_onto_an_occupied_path_is_tripwired(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    chap = bs_server.store.seed_chapter(book_id=book["id"], name="Recon")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
    run_grison("sync")
    # will try to relocate here
    _touch_remote_page(bs_server, page["id"], chapter_id=chap["id"])
    blocker = Path.cwd() / "methodology" / "library" / "playbook" / "recon" / "notes.md"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("unrelated local file already here", encoding="utf-8")

    result = run_grison("sync")

    assert "relocation target exists" in result.output
    old_path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    assert old_path.exists()
    assert blocker.read_text(encoding="utf-8") == "unrelated local file already here"


def test_force_local_bypasses_the_concurrent_drift_guard(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")
    path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
    _write_page_file(path, page_id=page["id"], title="Notes", book="playbook",
                      body="# Edited by human")

    def rename_on_bookstack() -> None:
        bs_server.store.page(page["id"])["name"] = "Renamed Concurrently"

    bs_server.on_request("GET", r"/api/pages/\d+", rename_on_bookstack, call_number=2)

    result = run_grison("sync", "--force-local", str(path))

    assert "methodology: pull 0, push 1, create 0" in result.output
    assert bs_server.store.page(page["id"])["markdown"] == "# Edited by human"


def test_mirror_first_materialization_has_the_readonly_header(run_grison, bs_server):
    bs_server.store.seed_book(name="Playbook", description="d")

    run_grison("sync")

    text = (Path.cwd() / "methodology" / "library" / "playbook" / ".book.yml").read_text(
        encoding="utf-8"
    )
    assert text.startswith("# READ-ONLY mirror")
