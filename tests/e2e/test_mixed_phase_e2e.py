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
