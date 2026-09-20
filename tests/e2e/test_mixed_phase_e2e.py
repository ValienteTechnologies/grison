"""End-to-end: all three phases (findings, reports, wiki) in one ``grison sync``
invocation.

Tests changed on purpose (task step 3, engine-findings): this file used to pin
"the findings phase always fails" — the historical, confirmed-live evidence-query
production break (``_EVIDENCE_QUERY`` selecting a ``findingId`` field ``evidence``
does not have; see ``tests/test_gw_schema_conformance.py``) — as this suite's
permanent findings-phase outcome. Findings are now engine-managed
(``grison.adapters.gw_findings``/``gw_evidence``) and that query no longer exists,
so with no findings/evidence seeded the findings phase is simply clean, same as
the other two. What this file actually proves — all three phases run together in
one sync, none blocking another, second sync fully clean with zero remote
mutations — is unchanged; only the obsolete "findings always fails" expectation
is gone.

The REAL phase order inside one ``grison sync`` (``grison.cli.sync``), as of the
D1 phase-reordering fix (coordinator correction: evidence used to sync inside the
findings phase, AFTER narrative sections — a reupload's re-push then landed a
whole sync late): report directories established/indexed -> each report's
``evidence/`` file set -> narrative sections + project notes -> findings (library,
then reported) -> wiki (each book's ``images/`` file set, then pages). Still ONE
engine run per record kind, still no second "reports" phase — evidence just moved
inside the existing one, before narrative/notes, and its own plans/summaries/
events now land in ``ReportsPhaseResult`` (``grison status``/``--json``'s "report"
phase), not ``FindingsPhaseResult`` — see ``grison.cli._run_reports_phase``'s
docstring for the full reasoning.
"""

from __future__ import annotations

from pathlib import Path

REPORT_SCOPES = [
    {
        "name": "Internal range",
        "scope": "10.0.0.0/24",
        "description": "",
        "disallowed": False,
        "requiresCaution": False,
    }
]


def test_all_three_phases_run_with_mixed_outcomes(run_grison, gw_server, bs_server):
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("sync")

    # findings: the historical evidence-query break is fixed — clean, not a failure
    assert "findings (gw.finding): clean" in result.output
    assert "findings (gw.reportedFinding): clean" in result.output
    assert "sync failed" not in result.output
    # reports: pulled the one seeded report's narrative (tests changed on purpose,
    # reports engine step: the reports phase's own summary line is now the engine's
    # "reports (<kind>): ..." counts line per kind, like the wiki's — see
    # grison.cli._print_reports_summary. All 7 of the lab-shaped extraFieldSpec
    # fields pull, not just the one seeded with content — a report's narrative
    # sections are the field SPEC, not just its populated keys)
    assert "reports (gw.reportSection): pull_new 7" in result.output
    assert "reports: create 1 report dir(s)" in result.output
    # wiki: pulled the one seeded page (tests changed on purpose: the wiki phase's
    # own summary line format — see test_cross_cutting_e2e's own note on
    # grison.cli._print_wiki_summary)
    assert "wiki (bs.page): pull_new 1" in result.output
    assert result.exit_code == 0  # every phase clean/pulled, nothing tainted

    # tests changed on purpose (D3/D4): a freshly-pulled report directory is named
    # slug(title) with no numeric id prefix at all — "7-report-a" was the v1 shape.
    assert (
        Path.cwd() / "findings" / "reports" / "report-a" / "narrative" / "executive_summary.md"
    ).exists()
    assert (Path.cwd() / "methodology" / "library" / "playbook" / "getting-started.md").exists()
    assert gw_server.operation_log == []
    assert bs_server.operation_log == []


def test_second_mixed_sync_reports_and_methodology_are_clean(run_grison, gw_server, bs_server):
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("sync")

    assert "findings (gw.finding): clean" in result.output
    assert "findings (gw.reportedFinding): clean" in result.output
    assert "reports (gw.reportSection): clean 7" in result.output
    assert "wiki (bs.page): clean 1" in result.output
    assert result.exit_code == 0  # every phase clean on the second sync too
    assert gw_server.operation_log == []
    assert bs_server.operation_log == []


def test_evidence_syncs_before_narrative_sections_and_findings_in_one_run(
    run_grison,
    gw_server,
):
    """The real phase order, proven directly (not just described): a report's
    evidence, its narrative section, and a reported finding all reference the
    SAME evidence row — evidence has to download before EITHER can resolve it,
    in this SAME first sync, or the narrative/finding would pull an
    unresolved-reference placeholder instead of the real image line."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/shot.png",
        friendlyName="shot",
        caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=7,
        title="SQLi",
        severityId=5,
        findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    report["extraFields"] = {
        **(report.get("extraFields") or {}),
        "executive_summary": '<div class="richtext-evidence" data-evidence-id="90"></div>',
    }

    result = run_grison("sync")

    assert "reports (gw.evidence[findings/reports/report-a]): pull_new 1" in result.output
    assert result.exit_code == 0, result.output

    narrative = (
        Path.cwd() / "findings" / "reports" / "report-a" / "narrative" / "executive_summary.md"
    )
    finding = Path.cwd() / "findings" / "reports" / "report-a" / "sqli.md"
    assert "![Login screen](evidence/shot.png)" in narrative.read_text(encoding="utf-8")
    assert "![Login screen](evidence/shot.png)" in finding.read_text(encoding="utf-8")
    # neither ever showed an unresolved-reference placeholder — proves evidence
    # was already downloaded+indexed by the time each classified/rendered
    assert "gw:evidence-ref" not in narrative.read_text(encoding="utf-8")
    assert "gw:evidence-ref" not in finding.read_text(encoding="utf-8")


def test_a_later_phase_crashing_still_leaves_an_undoable_snapshot_of_the_earlier_write(
    run_grison,
    gw_server,
    bs_server,
    monkeypatch,
):
    """Crash-durability (coordinator correction): the run's ONE undo snapshot is
    checkpointed (persisted in place, ``grison.cli.sync``'s ``_checkpoint_snapshot``)
    after EACH phase, not only once at the very end — persisting only once, after
    the last phase, would drop an earlier phase's already-applied remote write from
    the undo record if a later phase then crashes (or the process itself dies)
    before that final persist ever runs. Here the wiki phase raises right after the
    report phase has pushed a narrative section: the report phase's write must
    still be on disk in a snapshot, and a single ``grison undo`` must still reverse
    it, even though the sync that made the write never finished."""
    import grison.cli as cli_mod

    gw_server.store.extra_field_specs.clear()  # crisp counts — just this one field
    gw_server.store.seed_report_extra_field_specs(["executive_summary"])
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    first = run_grison("sync")  # clean — pulls everything, no remote write at all
    assert first.exit_code == 0, first.output

    section = (
        Path.cwd() / "findings" / "reports" / "report-a" / "narrative" / "executive_summary.md"
    )
    section.write_text("Edited by hand.\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "_run_wiki_phase", _boom)

    result = run_grison("sync")

    assert result.exit_code == 1, result.output
    assert "wiki sync failed: boom" in result.output
    assert "reports (gw.reportSection): push 1" in result.output, result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "Edited by hand." in report["extraFields"]["executive_summary"]  # the push landed

    snapshots_dir = Path.cwd() / ".grison" / "snapshots"
    names = sorted(p.name for p in snapshots_dir.iterdir())
    assert len(names) == 1  # checkpointed after the report phase despite the wiki crash

    undo_result = run_grison("undo")

    assert undo_result.exit_code == 0, undo_result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "Summary." in report["extraFields"]["executive_summary"]  # reversed
