"""End-to-end findings-phase (Ghostwriter library + reported findings + evidence)
scenarios, against the DECIDED behavior (engine step 3 of 3;
:mod:`grison.adapters.gw_findings`, :mod:`grison.adapters.gw_evidence`,
:mod:`grison.engine.filesets`). No strict-xfail trip-wires left: the production
break these used to document unconditionally (``GhostwriterClient.fetch_evidence``
selected a nonexistent ``evidence { findingId }`` field on the real >= 7.2 schema)
is fixed by the report-scoped evidence queries — see
``tests/test_gw_schema_conformance.py`` for the permanent schema-level check and
``lab/LAB.md`` (rework repo) for the confirmed live server error this documents.

Report directories are seeded directly into ``.grison/index.json``
(:func:`_seed_report_dir`) rather than through a real report-creation sync: the
report/narrative/notes phase (what actually creates a ``gw.report`` index entry)
is a DIFFERENT engine step's own responsibility, being built in a different
worktree at the same time this one was — these tests exercise the findings
phase's OWN adapters against a report directory that already exists and is
already indexed, exactly the shape the findings phase always finds it in
downstream of that other phase. (The old v1 ``grison.remote.reports`` module
still runs in this worktree too, since it isn't this task's to touch, but it
knows nothing about ``gw.report`` index entries — its own output would trip
IDX-003 if these tests didn't scope validation and event assertions to
``findings/reports/<seeded-name>/`` only.)
"""

from __future__ import annotations

from pathlib import Path

from grison.index import Index, IndexKind

REPORT_SCOPES = [{"name": "Internal range", "scope": "10.0.0.0/24", "description": "",
                   "disallowed": False, "requiresCaution": False}]


def _seed_report_dir(workspace: Path, report_id: int, name: str = "report-a") -> Path:
    """Index ``findings/reports/<name>`` as ``gw.report`` (D3: identity lives in
    the index, never parsed from a directory name) and create the directory."""
    rel = f"findings/reports/{name}"
    report_dir = workspace / rel
    report_dir.mkdir(parents=True, exist_ok=True)
    index = Index.load(workspace)
    index.set(rel, IndexKind.GW_REPORT, report_id)
    index.save()
    return report_dir


def test_pulled_library_finding_has_no_id_block(run_grison, gw_server):
    """D3: a pulled document carries no ``grison:`` block and no ids at all —
    identity lives in ``.grison/index.json``, never in the content document."""
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)

    result = run_grison("sync")

    assert result.exit_code == 0, result.output
    path = Path.cwd() / "findings" / "library" / "weak-tls-ciphers.md"
    text = path.read_text(encoding="utf-8")
    assert "grison:" not in text
    assert "id:" not in text
    index = Index.load(Path.cwd())
    rec = index.get("findings/library/weak-tls-ciphers.md")
    assert rec is not None and rec.kind is IndexKind.GW_FINDING and rec.id == 1


def test_library_finding_push_updates_ghostwriter(run_grison, gw_server, workspace):
    """A local edit to a pulled library finding pushes back through
    ``update_finding_by_pk`` — no id ever re-enters the document."""
    gw_server.store.seed_finding(
        id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4,
        description="<p>old text</p>",
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    path = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("old text", "brand new text"),
        encoding="utf-8",
    )
    second = run_grison("sync")

    assert "findings (gw.finding): push 1" in second.output, second.output
    row = gw_server.store._by_id(gw_server.store.findings, 1)
    assert row is not None and "brand new text" in row["description"]
    assert "grison:" not in path.read_text(encoding="utf-8")


def test_report_finding_evidence_pulls_as_image_line(run_grison, gw_server, workspace):
    """D1: the only authored evidence form is a markdown image line —
    ``![caption](evidence/file.png)`` — built from the report-level evidence row
    a native ``richtext-evidence`` div references, never a per-finding link (which
    does not exist in the real >= 7.2 schema)."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    _seed_report_dir(workspace, report["id"], "report-a")
    gw_server.store.seed_evidence(
        id=90, reportId=report["id"], friendlyName="login_screenshot",
        document=f"evidence/{report['id']}/login_screenshot.png", caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="SQL Injection", severityId=5, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): pull_new 1" in result.output, result.output
    finding_path = Path.cwd() / "findings" / "reports" / "report-a" / "sql-injection.md"
    body = finding_path.read_text(encoding="utf-8")
    assert '![Login screen](evidence/login_screenshot.png' in body
    assert "grison:" not in body and "id:" not in body
    evidence_file = Path.cwd() / "findings" / "reports" / "report-a" / "evidence" \
        / "login_screenshot.png"
    assert evidence_file.is_file()
    index = Index.load(Path.cwd())
    rec = index.get("findings/reports/report-a/evidence/login_screenshot.png")
    assert rec is not None and rec.kind is IndexKind.GW_EVIDENCE and rec.id == 90


def test_new_local_evidence_file_uploads_report_scoped(run_grison, gw_server, workspace):
    """D1(a)/B: a new file dropped in ``evidence/`` and referenced from a finding
    uploads through ``uploadEvidence(report: ...)`` — report-scoped, never
    ``finding:`` — with ``friendlyName`` set to the file's stem, and the
    referencing finding is re-pushed carrying the new evidence id."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _seed_report_dir(workspace, report["id"], "report-a")
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
    )
    first = run_grison("sync")
    assert "findings (gw.reportedFinding): pull_new 1" in first.output, first.output

    finding_path = report_dir / "finding-one.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\n", "## Description\n\n![A screenshot](evidence/shot.png)\n\n", 1,
    )
    finding_path.write_text(text, encoding="utf-8")
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "shot.png").write_bytes(b"\x89PNG-fake-bytes")

    result = run_grison("sync")

    assert "gw.evidence[findings/reports/report-a]): create 1" in result.output, result.output
    assert len(gw_server.store.evidence) == 1
    row = gw_server.store.evidence[0]
    assert row["reportId"] == report["id"]
    assert row["friendlyName"] == "shot"
    updated_finding = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert updated_finding is not None
    assert f'data-evidence-id="{row["id"]}"' in updated_finding["description"]


def test_affected_entities_is_always_p_wrapped_on_push(run_grison, gw_server, workspace):
    """BRIEF D: affected_entities is ALWAYS pushed ``<p>``-wrapped — the lab proved
    plain text breaks Ghostwriter's docx export."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _seed_report_dir(workspace, report["id"], "report-a")
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
    )
    first = run_grison("sync")
    assert "findings (gw.reportedFinding): pull_new 1" in first.output, first.output

    finding_path = report_dir / "finding-one.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace("severity:", "affected_entities: 10.0.0.5\nseverity:", 1)
    finding_path.write_text(text, encoding="utf-8")

    result = run_grison("sync")

    assert "gw.reportedFinding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row is not None
    assert row["affectedEntities"].strip().startswith("<p>")
    assert row["affectedEntities"].strip().endswith("</p>")


def test_mass_evidence_delete_is_guarded_and_announced(run_grison, gw_server, workspace):
    """D6: the ONE change guard covers evidence deletions too, scoped per report
    (never mixing one report's rows into another's guard math), and announces
    what it withheld — deleting every locally-mirrored evidence file at once must
    not silently wipe out a report's evidence rows."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    _seed_report_dir(workspace, report["id"], "report-a")
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
    )
    for i in range(8):
        gw_server.store.seed_evidence(
            reportId=report["id"], friendlyName=f"shot-{i}", document=f"evidence/shot-{i}.png",
        )
    first = run_grison("sync")  # establishes the local evidence/ mirror
    assert "gw.evidence[findings/reports/report-a]): pull_new 8" in first.output, first.output

    ev_dir = workspace / "findings" / "reports" / "report-a" / "evidence"
    for f in ev_dir.glob("*"):
        f.unlink()

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on gw.evidence" in result.output, result.output
    assert len(gw_server.store.evidence) == 8  # nothing was actually deleted while withheld


def test_undo_reverses_a_library_push_and_an_evidence_upload(run_grison, gw_server, workspace):
    """G: ``grison undo`` covers the findings phase too — the newest snapshot's
    remote writes (a library ``update_finding_by_pk`` and a report-scoped
    ``uploadEvidence``) are replayed in reverse through the Ghostwriter adapters:
    the finding's pre-image is restored and the uploaded evidence row deleted."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _seed_report_dir(workspace, report["id"], "report-a")
    gw_server.store.seed_finding(
        id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4,
        description="<p>old text</p>",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    lib_path = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    lib_path.write_text(
        lib_path.read_text(encoding="utf-8").replace("old text", "brand new text"),
        encoding="utf-8",
    )
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "shot.png").write_bytes(b"\x89PNG-fake-bytes")
    second = run_grison("sync")
    assert "findings (gw.finding): push 1" in second.output, second.output
    assert len(gw_server.store.evidence) == 1
    assert "brand new text" in gw_server.store._by_id(gw_server.store.findings, 1)["description"]

    undone = run_grison("undo")

    assert undone.exit_code == 0, undone.output
    assert gw_server.store.evidence == []  # the create was reversed (delete_evidence_by_pk)
    assert "old text" in gw_server.store._by_id(gw_server.store.findings, 1)["description"]
