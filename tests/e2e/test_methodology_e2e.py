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
