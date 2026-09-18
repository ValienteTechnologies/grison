"""End-to-end: all three phases (findings, reports, wiki) in one ``grison sync``
invocation, mixed outcomes.

Today's real, permanent shape of this: the findings phase always fails (see
``tests/e2e/test_findings_e2e.py`` — the evidence-query production break), which by
itself makes the overall exit code 1 — but the reports and wiki phases are fully
isolated from that failure (``grison.cli._run_phase`` catches per phase) and still
run, and still print their own clean summary lines.
"""

from __future__ import annotations

from pathlib import Path

REPORT_SCOPES = [{"name": "Internal range", "scope": "10.0.0.0/24", "description": "",
                   "disallowed": False, "requiresCaution": False}]


def test_all_three_phases_run_with_mixed_outcomes(run_grison, gw_server, bs_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")

    result = run_grison("sync")

    # findings: isolated failure, does not stop the phases after it
    assert (
        "findings sync failed: Ghostwriter GraphQL error: "
        "field 'findingId' not found in type: 'evidence'"
    ) in result.output
    # reports: succeeded cleanly despite the findings-phase failure right before it
    # (tests changed on purpose, reports engine step: the reports phase's own
    # summary line is now the engine's "reports (<kind>): ..." counts line per
    # kind, like the wiki's — see grison.cli._print_reports_summary. All 7 of the
    # lab-shaped extraFieldSpec fields pull, not just the one seeded with content —
    # a report's narrative sections are the field SPEC, not just its populated keys)
    assert "reports (gw.reportSection): pull_new 7" in result.output
    assert "reports: create 1 report dir(s)" in result.output
    # wiki: succeeded cleanly, isolated from both phases before it (tests changed on
    # purpose: the wiki phase's own summary line format — see test_cross_cutting_e2e's
    # own note on grison.cli._print_wiki_summary)
    assert "wiki (bs.page): pull_new 1" in result.output
    assert result.exit_code == 1  # tainted by the findings-phase failure alone

    # tests changed on purpose (D3/D4): a freshly-pulled report directory is named
    # slug(title) with no numeric id prefix at all — "7-report-a" was the v1 shape.
    assert (Path.cwd() / "findings" / "reports" / "report-a" / "narrative"
            / "executive_summary.md").exists()
    assert (Path.cwd() / "methodology" / "library" / "playbook" / "getting-started.md").exists()
    assert gw_server.operation_log == []
    assert bs_server.operation_log == []


def test_second_mixed_sync_reports_and_methodology_are_clean(run_grison, gw_server, bs_server):
    gw_server.store.seed_report(
        id=7, title="Report A", extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Getting Started", markdown="# Hi")
    run_grison("sync")

    result = run_grison("sync")

    assert "reports (gw.reportSection): clean 7" in result.output
    assert "wiki (bs.page): clean 1" in result.output
    assert result.exit_code == 1  # still tainted by the ever-present findings-phase failure
    assert gw_server.operation_log == []
    assert bs_server.operation_log == []
