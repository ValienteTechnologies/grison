"""End-to-end report-narrative sync scenarios — CLI + on-disk files + ``gw_server``'s
inspection API only. No import from ``grison.remote.reports``/``sync``/``state``, no
assertion on private state-file contents.

Every ``run_grison("sync")`` also runs the findings phase (engine-managed now —
see ``tests/e2e/test_findings_e2e.py``), which is clean by default whenever a
scenario here doesn't seed any library/reported findings itself. These tests
assert the reports phase's own summary line, the Ghostwriter operation log, and
the files on disk, rather than assuming anything about the overall exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPORT_SCOPES = [{"name": "Internal range", "scope": "10.0.0.0/24", "description": "",
                   "disallowed": False, "requiresCaution": False}]


def _rdir(report_id: int, slug: str) -> Path:
    return Path.cwd() / "findings" / "reports" / f"{report_id}-{slug}"


def test_first_sync_creates_narrative_project_and_notes(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<p>Summary text.</p>"},
        project={"codename": "OP-A", "scopes": REPORT_SCOPES,
                 "comments": [{"id": 1, "note": "<p>Kickoff note.</p>", "timestamp": "2026-01-02",
                               "operatorId": 1, "user": {"name": "Lab Admin", "username": "lab"}}]},
    )

    result = run_grison("sync")

    assert "reports: pull 1, push 0  (0 clean, 0 repaired)" in result.output
    rdir = _rdir(7, "report-a")
    assert (rdir / "narrative" / "executive_summary.md").read_text(encoding="utf-8").strip() \
        == "Summary text."
    assert (rdir / ".report.yml").exists()
    project_md = (rdir / "project.md").read_text(encoding="utf-8")
    assert "OP-A" in project_md
    assert "Internal range" in project_md
    notes = list((rdir / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "Kickoff note." in notes[0].read_text(encoding="utf-8")
    assert gw_server.operation_log == []  # a pull never mutates Ghostwriter


def test_second_sync_is_a_noop(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    before = section.read_text(encoding="utf-8")

    result = run_grison("sync")

    assert "reports: pull 0, push 0  (1 clean, 0 repaired)" in result.output
    assert section.read_text(encoding="utf-8") == before
    assert gw_server.operation_log == []


def test_local_edit_pushes_one_section(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.write_text("Edited by hand.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "reports: pull 0, push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "Edited by hand." in report["extraFields"]["executive_summary"]
    names = [o.name for o in gw_server.operation_log]
    assert names == ["update_report_by_pk"]


def test_remote_edit_pulls_one_section(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Changed on Ghostwriter.</p>"

    result = run_grison("sync")

    assert "reports: pull 1, push 0" in result.output
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    assert section.read_text(encoding="utf-8").strip() == "Changed on Ghostwriter."
    assert gw_server.operation_log == []


def test_dry_run_writes_nothing_and_reports_both_directions(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>", "methodology": "<p>Approach.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    exec_section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    meth_section = _rdir(7, "report-a") / "narrative" / "methodology.md"
    exec_section.write_text("Local edit.\n", encoding="utf-8")  # push candidate
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["methodology"] = "<p>Changed remotely.</p>"  # pull candidate

    result = run_grison("sync", "--dry-run")

    assert "would push" in result.output
    assert "would pull" in result.output
    assert exec_section.read_text(encoding="utf-8") == "Local edit.\n"
    assert meth_section.read_text(encoding="utf-8").strip() == "Approach."
    assert gw_server.operation_log == []


def test_collision_surfaced_then_force_flags(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.write_text("Local change.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Remote change.</p>"

    result = run_grison("sync")

    assert "collision" in result.output
    assert section.read_text(encoding="utf-8") == "Local change.\n"  # never overwritten
    sidecar = section.with_name("executive_summary.remote.md")
    assert sidecar.exists()
    assert "Remote change." in sidecar.read_text(encoding="utf-8")

    result = run_grison("sync", "--force-local", str(section))
    assert "reports: pull 0, push 1" in result.output
    assert "Local change." in report["extraFields"]["executive_summary"]


def test_force_remote_resolves_collision(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.write_text("Local change.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Remote change.</p>"
    run_grison("sync")  # surfaces the collision + sidecar

    result = run_grison("sync", "--force-remote", str(section))

    assert "reports: pull 1, push 0" in result.output
    assert section.read_text(encoding="utf-8").strip() == "Remote change."


def test_force_local_on_a_never_synced_hand_created_section(run_grison, gw_server):
    """A narrative file created by hand for an existing report field, with no merge
    base yet (never pulled/pushed) — reconciled the same "repair if it already
    matches, else collision" way as any other never-seen local file."""
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    (Path.cwd() / "findings" / "reports" / "7-report-a" / "narrative").mkdir(parents=True)
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.write_text("Hand-authored before any sync.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "collision" in result.output
    sidecar = section.with_name("executive_summary.remote.md")
    assert sidecar.exists()

    result = run_grison("sync", "--force-local", str(section))
    assert "reports: pull 0, push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "Hand-authored before any sync." in report["extraFields"]["executive_summary"]


def test_new_local_note_is_pushed(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    notes_dir = _rdir(7, "report-a") / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "team-note.md").write_text("A note for the team.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "notes: push 1" in result.output
    assert len(gw_server.store.project_notes) == 1
    assert "A note for the team." in gw_server.store.project_notes[0]["note"]
    pushed = [f for f in notes_dir.glob("*.md") if f.name != "team-note.md"]
    assert len(pushed) == 1  # renamed to <id>-<slug>.md, id-stamped, read-only mirror


def test_note_push_fails_loudly_when_operator_cannot_be_resolved(run_grison, gw_server):
    """If Ghostwriter's ``whoami`` names a user this token's ``user`` lookup can't
    find, the note push (and this report's whole apply step, isolated from any other
    report) fails loudly instead of guessing an operator id."""
    gw_server.store.users = []  # whoami's username now resolves to nobody
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={}, project={"id": 55, "scopes": REPORT_SCOPES},
    )
    notes_dir = _rdir(7, "report-a") / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "team-note.md").write_text("A note.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "could not resolve ghostwriter user id" in result.output.lower()
    assert gw_server.store.project_notes == []


def test_missing_scope_is_a_lint_not_a_block(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"codename": "OP-NOSCOPE", "scopes": []},
    )

    result = run_grison("sync")

    assert "report 7 (OP-NOSCOPE): project has no scope defined" in result.output
    assert "reports: pull 1, push 0" in result.output  # the sync itself still proceeds


def test_repair_when_both_sides_independently_converge(run_grison, gw_server):
    """Local and remote both changed to the SAME content — not a collision, just a
    stale base to restamp (mirrors the identical "repair" outcome findings/methodology
    reach when both sides drift to an identical value)."""
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.write_text("Converged text.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Converged text.</p>"

    result = run_grison("sync")

    assert "reports: pull 0, push 0  (0 clean, 1 repaired)" in result.output
    assert section.read_text(encoding="utf-8").strip() == "Converged text."
    assert gw_server.operation_log == []  # neither side needed a write, just a restamp


def test_mass_change_guard_withholds_and_announces_it(run_grison, gw_server):
    keys = [f"section_{i}" for i in range(8)]
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={k: f"<p>Body {i}.</p>" for i, k in enumerate(keys)},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    for k in keys[:7]:  # 7 of 8 sections edited: > 5 and > 0.5 * 8
        (_rdir(7, "report-a") / "narrative" / f"{k}.md").write_text("Edited.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on reports — pushes withheld." in result.output
    assert gw_server.operation_log == []  # every push was withheld


def test_malformed_local_file_report_isolated_from_other_reports(run_grison, gw_server):
    """A report whose apply step fails outright (here: an unresolvable note operator)
    does not stop a sibling report's own sync — per-report isolation."""
    gw_server.store.users = []
    gw_server.store.seed_report(
        id=7, title="Broken Report", extraFields={}, project={"id": 55, "scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_report(
        id=8, title="Good Report", extraFields={"executive_summary": "<p>Fine.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    (_rdir(7, "broken-report") / "notes").mkdir(parents=True)
    (_rdir(7, "broken-report") / "notes" / "note.md").write_text("Note.\n", encoding="utf-8")

    result = run_grison("sync")

    assert (_rdir(8, "good-report") / "narrative" / "executive_summary.md").exists()
    assert "could not resolve ghostwriter user id" in result.output.lower()


# --- narrative: sections appearing/disappearing, headings -----------------------


def test_new_section_appears_remotely_on_second_sync(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["methodology"] = "<p>Newly defined section.</p>"

    result = run_grison("sync")

    assert "reports: pull 1, push 0  (1 clean, 0 repaired)" in result.output
    new_section = _rdir(7, "report-a") / "narrative" / "methodology.md"
    assert new_section.read_text(encoding="utf-8").strip() == "Newly defined section."


def test_section_removed_remotely_kept_locally_persists_across_two_syncs(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<p>s</p>", "methodology": "<p>m</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    del report["extraFields"]["methodology"]
    meth_section = _rdir(7, "report-a") / "narrative" / "methodology.md"

    r1 = run_grison("sync")
    assert "remote section gone — kept locally" in r1.output
    assert meth_section.read_text(encoding="utf-8").strip() == "m"

    r2 = run_grison("sync")  # a second consecutive sync — must not degrade to "unknown field"
    assert "remote section gone — kept locally" in r2.output
    assert "unknown report field" not in r2.output


def test_section_deleted_locally_is_silently_repulled(run_grison, gw_server):
    """Current behavior, no delete-sync feature for narrative sections either
    (mirrors the same "deleting a tracked local file doesn't delete anything remote"
    doctrine as findings evidence and methodology pages): the file just comes back."""
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>s</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    section.unlink()

    result = run_grison("sync")

    assert "reports: pull 1, push 0" in result.output
    assert section.exists()


def test_heading_levels_round_trip_pull_and_push(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<h2>Overview</h2><h3>Detail</h3><p>Body.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    pulled = section.read_text(encoding="utf-8")
    assert "## Overview" in pulled
    assert "### Detail" in pulled

    section.write_text("## New Heading\n\nNew body.\n", encoding="utf-8")
    run_grison("sync")

    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "<h2>New Heading</h2>" in report["extraFields"]["executive_summary"]


# --- project.md / .report.yml: regeneration + hand-edit handling ----------------


def test_project_md_regenerates_when_remote_project_data_changes(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={},
        project={"codename": "OP-OLD", "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    ctx = _rdir(7, "report-a") / "project.md"
    assert "OP-OLD" in ctx.read_text(encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["project"]["codename"] = "OP-NEW"

    run_grison("sync")

    assert "OP-NEW" in ctx.read_text(encoding="utf-8")


def test_project_md_hand_edit_is_silently_overwritten_today(run_grison, gw_server):
    """Current behavior — project.md is a plain "regenerate if different" mirror with
    no hand-edit detection (unlike methodology's book/chapter mirrors, which DO detect
    and preserve a hand-edit via a sidecar). D11/validator work is what turns this into
    a loud validation failure instead of a silent overwrite; see the strict-xfail twin
    below for the decided behavior."""
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={},
        project={"codename": "OP-A", "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    ctx = _rdir(7, "report-a") / "project.md"
    ctx.write_text(ctx.read_text(encoding="utf-8") + "\n\nHand-added note.\n",
                    encoding="utf-8")

    run_grison("sync")  # remote project data hasn't changed at all

    assert "Hand-added note." not in ctx.read_text(encoding="utf-8")  # silently clobbered


@pytest.mark.xfail(
    strict=True,
    reason="D11/engine: read-only regenerated mirrors must detect a hand-edit and "
    "refuse (validation failure), not silently overwrite it — project.md/.report.yml "
    "currently have no hand-edit detection at all, unlike methodology's book/chapter "
    "mirrors",
)
def test_project_md_hand_edit_is_rejected_by_validation_once_decided(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={},
        project={"codename": "OP-A", "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    ctx = _rdir(7, "report-a") / "project.md"
    ctx.write_text(ctx.read_text(encoding="utf-8") + "\n\nHand-added note.\n",
                    encoding="utf-8")

    result = run_grison("sync")

    assert result.exit_code != 0
    assert "Hand-added note." in ctx.read_text(encoding="utf-8")  # never clobbered


def test_report_yml_hand_edit_is_silently_overwritten_today(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={}, project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    meta = _rdir(7, "report-a") / ".report.yml"
    edited = meta.read_text(encoding="utf-8").replace("title: Report A", "title: HAND-EDITED")
    meta.write_text(edited, encoding="utf-8")

    run_grison("sync")

    assert "HAND-EDITED" not in meta.read_text(encoding="utf-8")  # silently clobbered


@pytest.mark.xfail(
    strict=True,
    reason="D11/engine: same decided hand-edit-detection behavior as project.md above, "
    "applied to .report.yml",
)
def test_report_yml_hand_edit_is_rejected_by_validation_once_decided(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={}, project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    meta = _rdir(7, "report-a") / ".report.yml"
    edited = meta.read_text(encoding="utf-8").replace("title: Report A", "title: HAND-EDITED")
    meta.write_text(edited, encoding="utf-8")

    result = run_grison("sync")

    assert result.exit_code != 0
    assert "HAND-EDITED" in meta.read_text(encoding="utf-8")


# --- notes: never updated/deleted, a remote edit produces an orphaned extra mirror --


def test_existing_note_mirror_is_never_updated_when_the_remote_note_is_edited(
    run_grison, gw_server
):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={},
        project={
            "id": 55, "scopes": REPORT_SCOPES,
            "comments": [{"id": 10, "note": "<p>Original text</p>", "timestamp": "2026-01-02",
                          "operatorId": 1, "user": {"name": "Lab Admin", "username": "lab"}}],
        },
    )
    run_grison("sync")
    ndir = _rdir(7, "report-a") / "notes"
    original_files = sorted(ndir.glob("*.md"))
    assert len(original_files) == 1
    original_content = original_files[0].read_text(encoding="utf-8")
    assert "Original text" in original_content

    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["project"]["comments"][0]["note"] = "<p>Edited remotely</p>"
    run_grison("sync")

    after_files = sorted(ndir.glob("*.md"))
    assert len(after_files) == 2  # the edit produced a NEW mirror, old one untouched
    assert original_files[0] in after_files
    assert original_files[0].read_text(encoding="utf-8") == original_content  # never updated
    new_file = [f for f in after_files if f != original_files[0]][0]
    assert "Edited remotely" in new_file.read_text(encoding="utf-8")


# --- missing-scope lint does not block a sibling report -------------------------


def test_missing_scope_lint_does_not_block_a_sibling_report(run_grison, gw_server):
    gw_server.store.seed_report(
        id=5, title="No Scope", extraFields={"executive_summary": "<p>a</p>"},
        project={"codename": "OP-NOSCOPE", "scopes": []},
    )
    gw_server.store.seed_report(
        id=6, title="Has Scope", extraFields={"executive_summary": "<p>b</p>"},
        project={"scopes": REPORT_SCOPES},
    )

    result = run_grison("sync")

    assert "report 5 (OP-NOSCOPE): project has no scope defined" in result.output
    assert (_rdir(5, "no-scope") / "narrative" / "executive_summary.md").exists()
    assert (_rdir(6, "has-scope") / "narrative" / "executive_summary.md").exists()


# --- stale-push guard (_guard_stale_push), via the fake's request hook ----------


def test_stale_push_guard_aborts_push_as_collision_on_concurrent_edit(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<p>old summary</p>", "methodology": "<p>m1</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    meth = _rdir(7, "report-a") / "narrative" / "methodology.md"
    es.write_text("new summary\n", encoding="utf-8")  # local edit, this section alone would push

    def concurrent_edit() -> None:
        report = next(r for r in gw_server.store.reports if r["id"] == 7)
        report["extraFields"]["methodology"] = "<p>m2 concurrent</p>"

    # tests changed on purpose (task step 3): the CLI now runs the report phase
    # BEFORE findings (a brand-new report only becomes an indexed gw.report
    # directory during the report phase — see grison/cli.py's `sync`), and the
    # findings phase itself never queries "report" at all (gw.finding/
    # gw.reportedFinding carry their own reportId, no separate report fetch) —
    # so the guard's pre-push refetch is the 2nd "report" call THIS sync makes
    # (this phase's own top-of-run snapshot, then the guard's refetch), not the
    # 3rd a findings-phase report fetch used to make it — targeted relative to
    # the baseline already spent by the first sync above, rather than a
    # hardcoded absolute count.
    baseline = gw_server.call_count("report")
    gw_server.on_request("report", concurrent_edit, call_number=baseline + 2)

    result = run_grison("sync")

    assert "push withheld" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["extraFields"]["executive_summary"] == "<p>old summary</p>"  # withheld
    assert es.read_text(encoding="utf-8").strip() == "new summary"  # local edit survives, unsent
    assert "collision" in result.output
    assert meth.with_name("methodology.remote.md").read_text(encoding="utf-8").strip() \
        == "m2 concurrent"


def test_stale_push_guard_withholds_push_when_report_vanishes_mid_run(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>old</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    es.write_text("new\n", encoding="utf-8")

    def vanish() -> None:
        gw_server.store.reports[:] = [r for r in gw_server.store.reports if r["id"] != 7]

    baseline = gw_server.call_count("report")  # see call-count note above
    gw_server.on_request("report", vanish, call_number=baseline + 2)

    result = run_grison("sync")

    assert "no longer exists remotely" in result.output


# --- closing gaps vs. tests/test_reports.py / test_reports_guards.py -----------


def test_unknown_local_narrative_key_is_skipped_not_pushed(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>s</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    rogue = _rdir(7, "report-a") / "narrative" / "made_up_section.md"
    rogue.write_text("invented\n", encoding="utf-8")

    result = run_grison("sync")

    assert "unknown report field" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "made_up_section" not in report["extraFields"]  # never created remotely


def test_pull_surfaces_a_dropped_styling_construct_as_a_warning_once(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={
            "executive_summary": (
                '<p><span data-color="#ff0000" style="color: #ff0000;">urgent</span></p>'
            ),
        },
        project={"scopes": REPORT_SCOPES},
    )
    r1 = run_grison("sync")
    assert "styling span dropped" in r1.output

    r2 = run_grison("sync")  # nothing changed — must not re-warn every routine sync
    assert "styling span dropped" not in r2.output


def test_project_md_renders_excluded_and_caution_scope_flags(run_grison, gw_server):
    scopes = [
        {"name": "Internal", "scope": "10.0.0.0/8", "description": "", "disallowed": False,
         "requiresCaution": True},
        {"name": "Excluded hosts", "scope": "10.9.9.9", "description": "", "disallowed": True,
         "requiresCaution": False},
    ]
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={}, project={"scopes": scopes},
    )

    run_grison("sync")

    text = (_rdir(7, "report-a") / "project.md").read_text(encoding="utf-8")
    assert "### Internal (CAUTION)" in text
    assert "### Excluded hosts (EXCLUDED)" in text


def test_note_push_dry_run_inserts_nothing_and_leaves_the_file_untouched(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={}, project={"id": 55, "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    notes_dir = _rdir(7, "report-a") / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    new_note = notes_dir / "idea.md"
    new_note.write_text("A dry-run idea\n", encoding="utf-8")

    result = run_grison("sync", "--dry-run")

    assert "would push note" in result.output
    assert gw_server.store.project_notes == []
    assert new_note.read_text(encoding="utf-8") == "A dry-run idea\n"


def test_bad_narrative_html_is_isolated_other_reports_still_sync(run_grison, gw_server):
    gw_server.store.seed_report(
        id=5, title="Broken", extraFields={"scope_text": "<table><tr><td>x</td></tr></table>"},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_report(
        id=6, title="Good", extraFields={"executive_summary": "<p>fine</p>"},
        project={"scopes": REPORT_SCOPES},
    )

    result = run_grison("sync")

    assert "5" in result.output
    good = _rdir(6, "good") / "narrative" / "executive_summary.md"
    assert good.exists()
    assert not list((Path.cwd() / "findings" / "reports").glob("5-*"))  # never materialized


def test_push_merges_over_a_fresh_fetch_not_the_stale_top_of_run_snapshot(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>old</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    es.write_text("new\n", encoding="utf-8")

    def add_brand_new_field() -> None:
        report = next(r for r in gw_server.store.reports if r["id"] == 7)
        report["extraFields"]["new_field"] = "<p>brand new</p>"

    baseline = gw_server.call_count("report")  # see call-count note above
    gw_server.on_request("report", add_brand_new_field, call_number=baseline + 2)

    result = run_grison("sync")

    # the merge happens silently inside the single write — new_field never appears as
    # its own "pull" (it's merged over, not reconciled as a section of its own yet)
    assert "reports: pull 0, push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["extraFields"]["executive_summary"] == "<p>new</p>"
    assert report["extraFields"]["new_field"] == "<p>brand new</p>"  # never wiped by the push


def test_removed_remotely_marker_clears_once_the_local_file_is_deleted(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A",
        extraFields={"executive_summary": "<p>v1</p>", "methodology": "<p>m</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    meth = _rdir(7, "report-a") / "narrative" / "methodology.md"
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    del report["extraFields"]["methodology"]
    run_grison("sync")
    assert "remote section gone" in run_grison("sync").output

    meth.unlink()
    result = run_grison("sync")

    assert "remote section gone" not in result.output


def test_collision_persists_unresolved_across_a_second_sync(run_grison, gw_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>base</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir(7, "report-a") / "narrative" / "executive_summary.md"
    es.write_text("LOCAL\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>REMOTE</p>"
    run_grison("sync")  # first collision

    result = run_grison("sync")  # still unresolved — must keep surfacing, not silently drop it

    assert "collision" in result.output
    assert es.read_text(encoding="utf-8") == "LOCAL\n"
    assert es.with_name("executive_summary.remote.md").read_text(encoding="utf-8").strip() \
        == "REMOTE"
