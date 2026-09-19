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

_REPORT_SCOPES = [
    {
        "name": "Internal range",
        "scope": "10.0.0.0/24",
        "description": "",
        "disallowed": False,
        "requiresCaution": False,
    }
]


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

    # tests changed on purpose (task step 3): findings are engine-managed now
    # (grison.adapters.gw_findings/gw_evidence) — with nothing seeded, both
    # findings kinds report clean 0, the same shape methodology already had,
    # not the old "not yet engine-managed" placeholder line.
    assert result.exit_code == 0
    assert "findings (library): clean" in result.output
    assert "findings (reports): clean" in result.output
    assert "report: clean" in result.output
    assert "methodology: clean" in result.output
    assert "last findings sync: never" in result.output
    assert "last report sync: never" in result.output
    assert "last wiki sync: never" in result.output


# ---------------------------------------------------------------------------
# After a clean sync: last-sync-per-phase shim (item 6). Findings are
# engine-managed now (task step 3 fixed the historical fetch_evidence/
# findingId break — see tests/e2e/test_findings_e2e.py), so with nothing
# seeded the findings phase is clean too, same as reports/wiki.
# ---------------------------------------------------------------------------


def test_status_after_a_clean_sync_shows_clean_and_per_phase_last_sync(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("status")

    assert result.exit_code == 0
    assert "methodology: clean 1" in result.output
    assert "last wiki sync:" in result.output
    assert "last findings sync:" in result.output
    assert "last report sync:" in result.output
    assert result.output.count("(ok)") == 3  # findings, report, and wiki all clean


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
    assert "  invalid  methodology/library/playbook/getting-started.md (WIKI-002)" in result.output


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
    assert (
        "! methodology/library/playbook/getting-started.md: unresolved collision" in result.output
    )
    # the record underneath is unresolved-edited, not itself invalid/unknown
    assert "methodology: edited 1" in result.output


def test_status_reports_a_live_findings_collision_sidecar(run_grison, gw_server, workspace):
    """Item 12 (fix-fin1): before this fix, a library/reported-findings collision
    sidecar was invisible from `grison status` entirely — neither counted toward
    the exit code, nor surfaced under the top-level `findings` object in
    `--json` (unlike `report`/`methodology`'s own `collision_sidecars`), nor
    printed in text output."""
    gw_server.store.seed_finding(
        id=1,
        title="Weak TLS Ciphers",
        severityId=3,
        findingTypeId=4,
        description="<p>old text</p>",
    )
    run_grison("sync")
    path = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("old text", "local edit"), encoding="utf-8"
    )
    row = gw_server.store._by_id(gw_server.store.findings, 1)
    assert row is not None
    row["description"] = "<p>remote edit</p>"
    run_grison("sync")  # writes the .remote.md sidecar, never resolved

    result = run_grison("status")
    assert result.exit_code == 1, result.output  # a live collision sidecar IS a problem
    assert "findings: 1 collision-sidecar(s) pending" in result.output
    assert "! findings/library/weak-tls-ciphers.md: unresolved collision" in result.output

    json_result = run_grison("status", "--json")
    payload = json.loads(json_result.output)
    assert payload["findings"]["collision_sidecars"] == ["findings/library/weak-tls-ciphers.md"]


# ---------------------------------------------------------------------------
# --remote: will-pull/collision/remote-deleted via dry-run classify (item 4)
# ---------------------------------------------------------------------------


def test_status_remote_shows_a_dry_run_pull_without_writing_anything(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("status", "--remote")

    assert result.exit_code == 0  # a will-pull is not itself a problem
    # tests changed on purpose (item 1: --remote now dry-run classifies EVERY phase,
    # not just wiki, so the aggregate single-line "remote (--remote, dry-run): ..."
    # became one line per (phase, kind) — see grison.cli.status's own docstring).
    assert "remote wiki (bs.page): pull_new 1" in result.output
    # tests changed on purpose: --remote now reuses `grison sync`'s own phase
    # functions, which scope `validate_workspace` to a path that must already exist
    # (a deliberate validator invariant — "never silently treated as nothing to
    # check", grison/validator/core.py) — so `status --remote` ensures the same
    # plain, empty workspace directory tree `sync`'s own bootstrap always guarantees
    # (`grison.workspace.bootstrap_tree`; no FILE is ever created by it). No page
    # file was written under it either way — that's the real "nothing written" claim.
    assert (Path.cwd() / "methodology").is_dir()
    assert not any((Path.cwd() / "methodology").rglob("*.md"))
    assert bs_server.operation_log == []  # dry-run never mutates BookStack


# ---------------------------------------------------------------------------
# --remote now dry-run classifies Ghostwriter's phases too (item 1), not just
# the BookStack/wiki side: report -> findings, same phase functions `grison
# sync` itself uses, in dry-run mode.
# ---------------------------------------------------------------------------


def test_status_remote_reports_a_pending_push_and_a_pending_pull_and_writes_nothing(
    run_grison, gw_server, bs_server
):
    from tests.conftest import tree_snapshot

    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)
    book = bs_server.store.seed_book(name="Playbook")
    page = bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")  # pulls both, so there's something to edit on each side

    lib_path = Path.cwd() / "findings" / "library" / "weak-tls-ciphers.md"
    lib_path.write_text(
        lib_path.read_text(encoding="utf-8").replace("Weak TLS", "Locally-Edited Weak TLS"),
        encoding="utf-8",
    )
    bs_server.store.edit_page(page["id"], markdown="# Hi\n\nEdited on the server.")

    root = Path.cwd()
    before = tree_snapshot(root)
    gw_ops_before = len(gw_server.operation_log)
    bs_ops_before = len(bs_server.operation_log)

    result = run_grison("status", "--remote")

    assert result.exit_code == 0  # a pending push/pull is not itself a problem
    assert "remote findings (gw.finding): push 1" in result.output, result.output
    assert "remote wiki (bs.page): pull 1" in result.output, result.output
    # dry-run: writes NOTHING anywhere — no state, no snapshot, no sidecars, no
    # index save, no remote mutation (verified byte-for-byte, not just "still valid
    # JSON") — the exact guarantee item 1 asks for.
    assert tree_snapshot(root) == before
    assert len(gw_server.operation_log) == gw_ops_before
    assert len(bs_server.operation_log) == bs_ops_before


def test_status_remote_json_shape(run_grison, gw_server, bs_server):
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    lib_path = Path.cwd() / "findings" / "library" / "weak-tls-ciphers.md"
    lib_path.write_text(
        lib_path.read_text(encoding="utf-8").replace("Weak TLS", "Edited Weak TLS"),
        encoding="utf-8",
    )

    result = run_grison("status", "--remote", "--json")
    payload = json.loads(result.output)

    remote = payload["remote"]
    assert set(remote) == {"report", "findings", "wiki"}
    # report/findings/wiki: each either {"kinds": {<kind>: {"counts": ..., "problem_paths": ...}}}
    # (that leg's credentials worked and it ran) or a plain error string (credentials
    # missing / the leg failed before classifying) — never both shapes mixed for one
    # phase.
    assert remote["findings"]["kinds"]["gw.finding"]["counts"] == {"push": 1}
    assert remote["findings"]["kinds"]["gw.finding"]["problem_paths"] == []
    assert "kinds" in remote["report"]
    assert "kinds" in remote["wiki"]


def test_status_remote_json_is_valid_json_when_a_new_book_would_be_created(
    run_grison, bs_server
) -> None:
    """Item 8 (fix-fin1) regression: ``status --remote``'s dry-run classify calls
    the SAME wiki-phase structure pass ``sync`` does — before this fix, a brand
    new local book/chapter directory made that pass print its own raw
    ``typer.secho`` progress lines ("would create book …") REGARDLESS of
    ``--json``, corrupting the JSON output with leading plain text. The earlier
    ``test_status_remote_json_shape`` never caught this because it only edits an
    ALREADY-synced book (no structure event fires); this one seeds a brand-new
    book so the structure pass has something to report."""
    run_grison("sync")  # establishes the workspace with nothing to sync yet
    book_dir = Path.cwd() / "methodology" / "library" / "new-book"
    book_dir.mkdir(parents=True)
    (book_dir / "notes.md").write_text("---\ntitle: Notes\n---\n\n# Notes\n", encoding="utf-8")

    result = run_grison("status", "--remote", "--json")

    payload = json.loads(result.output)  # raises if any stray text preceded the JSON
    wiki_counts = payload["remote"]["wiki"]["kinds"]["bs.page"]["counts"]
    assert sum(wiki_counts.values()) == 1  # the new page was classified, one way or another


def test_status_remote_reports_missing_credentials_per_leg(run_grison, workspace):
    (workspace / ".grison" / "env").write_text(
        "GRISON_GW_URL=\nGRISON_GW_TOKEN=\nGRISON_BS_URL=\n"
        "GRISON_BS_TOKEN_ID=\nGRISON_BS_TOKEN_SECRET=\n",
        encoding="utf-8",
    )

    json_result = run_grison("status", "--remote", "--json")
    payload = json.loads(json_result.output)

    assert json_result.exit_code == 0  # missing creds is informational, not a "problem"
    assert payload["remote"]["report"] == "Ghostwriter credentials not configured"
    assert payload["remote"]["findings"] == "Ghostwriter credentials not configured"
    assert payload["remote"]["wiki"] == "BookStack credentials not configured"

    text_result = run_grison("status", "--remote")
    assert text_result.exit_code == 0
    assert "remote (ghostwriter): Ghostwriter credentials not configured" in text_result.output
    assert "remote (bookstack): BookStack credentials not configured" in text_result.output


# ---------------------------------------------------------------------------
# --json: stable shape (golden output)
# ---------------------------------------------------------------------------


def test_status_json_stable_shape(run_grison, bs_server):
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("status", "--json")
    payload = json.loads(result.output)

    # tests changed on purpose (task step 3 + reports task C): findings AND report
    # (narrative sections/notes) are both engine-managed now, each with the same
    # per-kind counts/non_clean shape methodology already had (findings splits into
    # a library bucket and a reports bucket; report/narrative sections+notes get
    # their own top-level area, same shape as methodology's, PLUS a sidecar-aware
    # `evidence` breakdown, D1's file-set mirror — moved here from `findings`
    # since evidence syncs on the report phase now, see fix item 5).
    assert payload["findings"]["managed"] is True
    assert payload["findings"]["collision_sidecars"] == []  # item 12, fix-fin1
    zero_counts = {
        "clean": 0,
        "edited": 0,
        "new": 0,
        "deleted": 0,
        "moved": 0,
        "invalid": 0,
        "unknown": 0,
    }
    assert payload["findings"]["library"] == {"counts": zero_counts, "non_clean": []}
    assert payload["findings"]["reports"]["counts"] == zero_counts
    assert payload["findings"]["reports"]["non_clean"] == []
    assert payload["report"]["managed"] is True
    assert payload["report"]["counts"] == zero_counts
    assert payload["methodology"]["managed"] is True
    assert payload["methodology"]["counts"] == {
        "clean": 1,
        "edited": 0,
        "new": 0,
        "deleted": 0,
        "moved": 0,
        "invalid": 0,
        "unknown": 0,
    }
    assert payload["methodology"]["non_clean"] == []
    assert payload["methodology"]["collision_sidecars"] == []
    assert payload["remote"] is None
    assert payload["last_sync"]["wiki"]["ok"] is True
    assert payload["last_sync"]["findings"]["ok"] is True
    assert payload["last_sync"]["report"]["ok"] is True


# ---------------------------------------------------------------------------
# Item 5 (fix-d): evidence lives under `report`, sidecar-aware, not under
# `findings` (a leftover from before evidence moved onto the report phase).
# ---------------------------------------------------------------------------


def test_status_shows_evidence_under_report_with_sidecar_aware_counts(run_grison, gw_server):
    """A live collision sidecar (``<name>.remote.<ext>``, ENGINE.md §8) is listed
    as a collision, never counted as a file — and the whole breakdown lives under
    ``report``, matching where evidence actually syncs now (D1: the report phase,
    not findings — see ``grison.adapters.gw_evidence``)."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": _REPORT_SCOPES})
    gw_server.store.seed_evidence(id=90, reportId=7, document="evidence/7/shot.png")
    run_grison("sync")

    evidence_dir = Path.cwd() / "findings" / "reports" / "report-a" / "evidence"
    assert (evidence_dir / "shot.png").is_file()
    # a stale collision sidecar left behind by some earlier, unresolved sync —
    # never a real evidence file (ENGINE.md §8).
    (evidence_dir / "shot.remote.png").write_bytes(b"stale collision sidecar")

    result = run_grison("status")

    assert result.exit_code == 0, result.output
    ev_line = next(ln for ln in result.output.splitlines() if ln.startswith("evidence:"))
    assert "findings/reports/report-a 1" in ev_line  # one real file, not two
    assert "1 collision-sidecar(s) pending" in ev_line

    json_result = run_grison("status", "--json")
    payload = json.loads(json_result.output)
    assert "evidence_files" not in payload["findings"]["reports"]  # moved out (item 5)
    assert payload["report"]["evidence"] == {
        "findings/reports/report-a": {"files": 1, "collisions": 1},
    }
