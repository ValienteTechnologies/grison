"""End-to-end findings-phase (Ghostwriter library + reported findings) scenarios.

Today, EVERY findings-phase sync fails unconditionally, regardless of data:
``GhostwriterClient.fetch_evidence`` selects ``evidence { findingId }``, and the real
Ghostwriter >= 7.2 schema has no ``findingId`` field on ``evidence`` at all (D1:
evidence belongs to a report, not a finding). ``fetch_evidence()`` is called
unconditionally near the top of both ``grison.remote.sync.pull`` and
``grison.remote.sync.sync``, before any record is even looked at — so this is a
100%-reproducible, data-independent break, not a scenario-specific one. See
``tests/test_gw_schema_conformance.py`` for the permanent schema-level check, and
``lab/LAB.md`` (rework repo) for the confirmed live server error.

Because of that break, no findings-phase scenario that goes through ``grison sync``
can currently reach D1 (evidence image lines)/D3 (no ids in documents) behavior — every
test below other than the first is written against the DECIDED behavior and marked
``xfail(strict=True)`` naming the checklist item that fixes it. Strict xfail is
deliberate: the day the engine consolidation lands, these go red until the marker is
removed, which is the point — a trip-wire, not a skipped test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPORT_SCOPES = [{"name": "Internal range", "scope": "10.0.0.0/24", "description": "",
                   "disallowed": False, "requiresCaution": False}]


def test_fake_rejects_todays_evidence_query_like_the_real_server(run_grison, gw_server):
    """Documents the production break end to end, through the real CLI: an otherwise
    completely empty workspace still fails the findings phase, because
    ``fetch_evidence()`` is unconditional. The exact message is the real Ghostwriter
    7.2.6 lab server's response, confirmed live (see ``lab/LAB.md``):
    ``field 'findingId' not found in type: 'evidence'``. Proves the fake is faithful,
    not just permissive."""
    result = run_grison("sync")

    assert (
        "findings sync failed: Ghostwriter GraphQL error: "
        "field 'findingId' not found in type: 'evidence'"
    ) in result.output
    assert result.exit_code == 1


@pytest.mark.xfail(
    strict=True,
    reason="D1/engine: fetch_evidence must select reportId (not findingId) before any "
    "reported-finding sync can complete at all",
)
def test_report_finding_evidence_pulls_as_image_line(run_grison, gw_server):
    """D1: the only authored evidence form is a markdown image line in the body —
    ``![caption](evidence/file.png "description")`` — built from the report-level
    evidence row referenced by the finding, not a per-finding link that does not
    exist in the real schema."""
    report = gw_server.store.seed_report(
        id=7, title="Report A", project={"scopes": REPORT_SCOPES},
    )
    finding = gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="SQL Injection", severityId=5, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    gw_server.store.seed_evidence(
        id=90, reportId=report["id"], friendlyName="login_screenshot",
        document=f"evidence/{report['id']}/login_screenshot.png", caption="Login screen",
    )

    result = run_grison("sync")

    assert result.exit_code == 0
    finding_path = (
        Path.cwd() / "findings" / "reports" / f"{report['id']}-report-a"
        / f"{finding['id']}-sql-injection.md"
    )
    body = finding_path.read_text(encoding="utf-8")
    assert '![Login screen](evidence/login_screenshot.png' in body


@pytest.mark.xfail(
    strict=True, reason="D3/engine: documents carry no machine fields — identity moves "
    "to a git-tracked grison-owned index file outside .grison/",
)
def test_pulled_library_finding_has_no_id_block(run_grison, gw_server):
    """D3: a pulled document carries no ``grison:`` block and no ids at all — identity
    lives in a separate index file, not in the content document."""
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)

    result = run_grison("sync")

    assert result.exit_code == 0
    path = Path.cwd() / "findings" / "library" / "weak-tls-ciphers.md"
    text = path.read_text(encoding="utf-8")
    assert "grison:" not in text


@pytest.mark.xfail(
    strict=True,
    reason="D6/engine: the mass-change guard must also cover a batch of evidence "
    "deletions, and say so — sync.py's _push_evidence deletes unclaimed remote rows "
    "with no guard and no announcement at all today (also blocked on the evidence "
    "query break above, since no reported-finding sync can complete)",
)
def test_mass_evidence_delete_is_guarded_and_announced(run_grison, gw_server):
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding", severityId=3, findingTypeId=4,
    )
    for i in range(8):
        gw_server.store.seed_evidence(
            reportId=report["id"], friendlyName=f"shot-{i}", document=f"evidence/{i}.png",
        )
    run_grison("sync")  # establishes the local evidence/ mirror
    ev_dir = (
        Path.cwd() / "findings" / "reports" / f"{report['id']}-report-a" / "evidence"
    )
    for f in ev_dir.glob("*"):
        f.unlink()  # remove every mirrored evidence file locally

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD" in result.output and "evidence" in result.output.lower()
    assert len(gw_server.store.evidence) == 8  # nothing was actually deleted while withheld
