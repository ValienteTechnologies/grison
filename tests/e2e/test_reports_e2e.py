"""End-to-end report-narrative sync scenarios — CLI + on-disk files + ``gw_server``'s
inspection API only. No import from ``grison.remote.reports``/``sync``/``state``, no
assertion on private state-file contents.

Every ``run_grison("sync")`` also runs the findings phase, which today fails
unconditionally before touching any record (see ``tests/e2e/test_findings_e2e.py``) —
that failure is isolated per phase and always makes the *overall* exit code 1 and
prints "findings sync failed: …", even when the reports phase itself is perfectly
clean. These tests assert the reports phase's own summary line, the Ghostwriter
operation log, and the files on disk rather than the overall exit code.
"""

from __future__ import annotations

from pathlib import Path

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
