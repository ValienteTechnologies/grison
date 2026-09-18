"""``grison status`` (coordinator feedback item 6) — CLI + on-disk files + fakes
only, same harness as ``test_methodology_e2e.py``.

Offline by default: methodology/ (on the sync engine) gets a real per-record
breakdown computed from the index and private state alone, no network — these
tests never seed ``bs_server`` unless a test explicitly passes ``--remote``. Every
non-clean bucket (edited/new/deleted/moved/invalid/unknown) and the live-collision-
sidecar signal get one golden-output test each; two tests cover the per-phase
last-sync shim (item 6's "old-module phases must write their outcome there too") and
the stable ``--json`` shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def _write_page_file(path: Path, *, title: str, body: str) -> None:
    fm = {"title": title}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False).strip() + f"\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Before any sync
# ---------------------------------------------------------------------------


def test_status_before_any_sync(run_grison):
    result = run_grison("status")

    assert result.exit_code == 0
    # tests changed on purpose (reports task C): findings/reports moved onto the
    # sync engine — `grison status` now gives it a real per-record breakdown (like
    # methodology), so the "not yet engine-managed" line is library-only.
    assert "findings: 0 library file(s) — not yet engine-managed" in result.output
    assert "reports: clean" in result.output
    assert "methodology: clean" in result.output
    assert "last findings sync: never" in result.output
    assert "last report sync: never" in result.output
    assert "last wiki sync: never" in result.output


# ---------------------------------------------------------------------------
# After a clean sync: last-sync-per-phase shim (item 6), including the findings
# phase's real, unconditional failure in this fake environment (see the module
# docstring of test_methodology_e2e.py) — status must show it, not hide it.
# ---------------------------------------------------------------------------


def test_status_after_a_clean_sync_shows_clean_and_per_phase_last_sync(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("status")

    assert result.exit_code == 0  # a historical findings-phase failure never poisons status
    assert "methodology: clean 1" in result.output
    assert "last wiki sync:" in result.output
    assert "(ok)" in result.output
    assert "last findings sync:" in result.output
    assert "FAILED" in result.output  # the fake's real, documented fetch_evidence bug
    assert "last report sync:" in result.output
    assert "(ok)" in result.output  # the report phase itself is clean in the fakes


# ---------------------------------------------------------------------------
# Offline buckets: edited / new / deleted
# ---------------------------------------------------------------------------


def test_status_reports_edited_new_and_deleted(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    bs_server.store.seed_page(book_id=book["id"], name="Going Away", markdown="# Bye")
    run_grison("sync")

    edited_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(edited_path, title="Getting Started", body="# Hi\n\nEdited by hand.")
    deleted_path = Path.cwd() / "methodology/library/playbook/going-away.md"
    deleted_path.unlink()
    new_path = Path.cwd() / "methodology/library/playbook/brand-new.md"
    _write_page_file(new_path, title="Brand New", body="# New page, never synced.")

    result = run_grison("status")

    assert result.exit_code == 0  # pending edits/new/deleted are not "problems"
    assert "methodology: edited 1, new 1, deleted 1" in result.output
    assert "  edited   methodology/library/playbook/getting-started.md" in result.output
    assert "  new      methodology/library/playbook/brand-new.md" in result.output
    assert "  deleted  methodology/library/playbook/going-away.md" in result.output


# ---------------------------------------------------------------------------
# Offline bucket: moved (identity pairing, identical content, no remote needed)
# ---------------------------------------------------------------------------


def test_status_reports_a_pure_move(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    old_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    new_path = old_path.with_name("relocated.md")
    old_path.rename(new_path)

    result = run_grison("status")

    assert result.exit_code == 0  # a pure move is not itself a problem
    assert "methodology: moved 1" in result.output
    assert (
        "  moved    methodology/library/playbook/relocated.md "
        "(from methodology/library/playbook/getting-started.md)"
    ) in result.output


# ---------------------------------------------------------------------------
# Offline bucket: invalid, with rule ids as the reason (item 6's explicit ask)
# ---------------------------------------------------------------------------


def test_status_reports_invalid_with_rule_ids(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, title="", body="# Hi")

    result = run_grison("status")

    assert result.exit_code == 1  # invalid IS a problem
    assert "methodology: invalid 1" in result.output
    assert "  invalid  methodology/library/playbook/getting-started.md (WIKI-002)" \
        in result.output


# ---------------------------------------------------------------------------
# Live collision sidecar
# ---------------------------------------------------------------------------


def test_status_reports_a_live_collision_sidecar(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")
    page_path = Path.cwd() / "methodology/library/playbook/getting-started.md"
    _write_page_file(page_path, title="Getting Started", body="# Hi\n\nLocal change.")
    bs_server.store.edit_page(page["id"], markdown="# Hi\n\nRemote change.")
    run_grison("sync")  # writes the .remote.md sidecar, never resolved

    result = run_grison("status")

    assert result.exit_code == 1  # a live collision sidecar IS a problem
    assert "1 collision-sidecar(s) pending" in result.output
    # named by the ORIGINAL record's path (what the user recognizes), not the
    # sidecar file's own name — the message spells out that it's a collision
    assert "! methodology/library/playbook/getting-started.md: unresolved collision" \
        in result.output
    # the record underneath is unresolved-edited, not itself invalid/unknown
    assert "methodology: edited 1" in result.output


# ---------------------------------------------------------------------------
# --remote: will-pull/collision/remote-deleted via dry-run classify (item 4)
# ---------------------------------------------------------------------------


def test_status_remote_shows_a_dry_run_pull_without_writing_anything(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("status", "--remote")

    assert result.exit_code == 0  # a will-pull is not itself a problem
    assert "remote (--remote, dry-run): pull_new 1" in result.output
    assert not (Path.cwd() / "methodology").exists()  # dry-run: nothing written
    assert bs_server.operation_log == []  # dry-run never mutates BookStack


# ---------------------------------------------------------------------------
# --json: stable shape (golden output)
# ---------------------------------------------------------------------------


def test_status_json_stable_shape(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("status", "--json")
    payload = json.loads(result.output)

    # tests changed on purpose (reports task C): reports is now its own engine-managed
    # area with the same shape as methodology's, no longer folded into "findings".
    assert payload["findings"] == {"managed": False, "library_files": 0}
    assert payload["reports"]["managed"] is True
    assert payload["reports"]["counts"] == {
        "clean": 0, "edited": 0, "new": 0, "deleted": 0, "moved": 0, "invalid": 0, "unknown": 0,
    }
    assert payload["methodology"]["managed"] is True
    assert payload["methodology"]["counts"] == {
        "clean": 1, "edited": 0, "new": 0, "deleted": 0, "moved": 0, "invalid": 0, "unknown": 0,
    }
    assert payload["methodology"]["non_clean"] == []
    assert payload["methodology"]["collision_sidecars"] == []
    assert payload["remote"] is None
    assert payload["last_sync"]["wiki"]["ok"] is True
    assert payload["last_sync"]["findings"]["ok"] is False
    assert "error" in payload["last_sync"]["findings"]
    assert payload["last_sync"]["report"]["ok"] is True
