"""End-to-end findings-phase (Ghostwriter library + reported findings + evidence)
scenarios, against the DECIDED behavior (engine step 3 of 3;
:mod:`grison.adapters.gw_findings`, :mod:`grison.adapters.gw_evidence`,
:mod:`grison.engine.filesets`). No strict-xfail trip-wires left: the production
break these used to document unconditionally (``GhostwriterClient.fetch_evidence``
selected a nonexistent ``evidence { findingId }`` field on the real >= 7.2 schema)
is fixed by the report-scoped evidence queries — see
``tests/test_gw_schema_conformance.py`` for the permanent schema-level check and
``lab/LAB.md`` (rework repo) for the confirmed live server error this documents.

Report directories come from a real ``run_grison("sync")`` now — the reports
adapter (:mod:`grison.adapters.gw_report`) is merged in, and ``grison.cli``'s
``sync`` command runs the report phase BEFORE the findings phase (a brand-new
report only becomes an indexed ``gw.report`` directory during the report phase;
see ``grison/cli.py``'s ``sync``), so a report seeded on the fake with no
``extraFieldSpec`` rows (the default — nothing pulls for it) is enough: one sync
creates the ``findings/reports/<slug>/`` directory + index entry AND, in the
same run, pulls whatever findings/evidence were also seeded into it.
"""

from __future__ import annotations

import json
from pathlib import Path

from grison.index import Index, IndexKind

REPORT_SCOPES = [
    {
        "name": "Internal range",
        "scope": "10.0.0.0/24",
        "description": "",
        "disallowed": False,
        "requiresCaution": False,
    }
]


def _rdir(workspace: Path, name: str = "report-a") -> Path:
    """Where the report phase creates a report directory for a slugged title
    (D3/D4: ``slug(title)``, no numeric id prefix) — computed, not seeded."""
    return workspace / "findings" / "reports" / name


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
        id=1,
        title="Weak TLS Ciphers",
        severityId=3,
        findingTypeId=4,
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
    gw_server.store.seed_evidence(
        id=90,
        reportId=report["id"],
        friendlyName="login_screenshot",
        document=f"evidence/{report['id']}/login_screenshot.png",
        caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="SQL Injection",
        severityId=5,
        findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): pull_new 1" in result.output, result.output
    finding_path = Path.cwd() / "findings" / "reports" / "report-a" / "sql-injection.md"
    body = finding_path.read_text(encoding="utf-8")
    assert "![Login screen](evidence/login_screenshot.png" in body
    assert "grison:" not in body and "id:" not in body
    evidence_file = (
        Path.cwd() / "findings" / "reports" / "report-a" / "evidence" / "login_screenshot.png"
    )
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
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
    )
    first = run_grison("sync")
    assert "findings (gw.reportedFinding): pull_new 1" in first.output, first.output

    finding_path = report_dir / "finding-one.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\n",
        "## Description\n\n![A screenshot](evidence/shot.png)\n\n",
        1,
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
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
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
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
    )
    for i in range(8):
        gw_server.store.seed_evidence(
            reportId=report["id"],
            friendlyName=f"shot-{i}",
            document=f"evidence/shot-{i}.png",
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
    """G: ``grison undo`` covers the findings phase too — a library
    ``update_finding_by_pk`` and a report-scoped ``uploadEvidence`` from the SAME
    ``grison sync`` are each replayed in reverse through the Ghostwriter adapters:
    the finding's pre-image is restored and the uploaded evidence row deleted.

    ``grison sync`` persists ONE undo snapshot for the whole run, shared across the
    report, findings and wiki phases (grison.engine.undo's module docstring: "the
    user's mental model is 'undo the last sync'") — even though evidence file sets
    sync in the REPORTS phase and the library push happens in the FINDINGS phase
    that follows it, both land in the SAME snapshot. A single ``grison undo``
    therefore reverses both writes from one sync, newest write first."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    gw_server.store.seed_finding(
        id=1,
        title="Weak TLS Ciphers",
        severityId=3,
        findingTypeId=4,
        description="<p>old text</p>",
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
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

    snapshots_dir = workspace / ".grison" / "snapshots"
    names = sorted(p.name for p in snapshots_dir.iterdir())
    assert len(names) == 1  # one snapshot for the whole run, not one per phase

    result = run_grison("undo")  # no name given -> the run's one snapshot

    assert result.exit_code == 0, result.output
    assert "old text" in gw_server.store._by_id(gw_server.store.findings, 1)["description"]
    assert gw_server.store.evidence == []  # the create was reversed too, same undo


def test_n_new_evidence_uploads_fetch_the_org_wide_evidence_list_only_once(
    run_grison, gw_server, workspace,
):
    """Regression for the fetch_evidence() thundering herd: BEFORE the fix,
    GwEvidenceAdapter._dedupe_friendly_name_for called the org-wide
    ``fetch_evidence()`` query once per uploaded file, and cli.py's post-sync
    ``evidence_by_report`` rebuild called ``list_remote()`` (another org-wide
    query) once per report — N uploads cost N-or-more fetches. Fixed by GWContext
    caching the evidence list for the run (:meth:`GWContext.all_evidence`) and
    GwEvidenceAdapter keeping it updated locally as it uploads."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    run_grison("sync")  # creates the report directory

    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (report_dir / "evidence" / f"shot-{i}.png").write_bytes(f"bytes-{i}".encode())

    before = len(gw_server.request_log)
    result = run_grison("sync")

    assert "gw.evidence[findings/reports/report-a]): create 3" in result.output, result.output
    assert len(gw_server.store.evidence) == 3
    evidence_fetches = [op for op in gw_server.request_log[before:] if op.name == "evidence"]
    assert len(evidence_fetches) == 1, evidence_fetches


def test_undo_refuses_to_guess_the_report_for_an_evidence_restore_with_no_reportid(
    run_grison, gw_server, workspace,
):
    """D3/undo safety: grison.engine.undo.replay's adapter map holds ONE
    GwEvidenceAdapter per bare kind string (bound to report_id=0 — see
    grison/cli.py's undo wiring), so GwEvidenceAdapter.restore can never trust
    self.report_id; the scope must come from the preimage alone. A preimage with
    no reportId at all (a corrupt or pre-D1 snapshot) must be refused with a
    GrisonError naming the file, never silently restored into report_id=0 or any
    other guessed scope."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    run_grison("sync")  # creates the report directory
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "shot.png").write_bytes(b"\x89PNG-fake-bytes")
    run_grison("sync")  # uploads the evidence row
    assert len(gw_server.store.evidence) == 1

    # a locally-deleted (indexed) evidence file deletes the remote row too (D1(a))
    # -- the delete_remote undo op's preimage is what gets corrupted below.
    (report_dir / "evidence" / "shot.png").unlink()
    run_grison("sync")
    assert gw_server.store.evidence == []

    snapshots_dir = workspace / ".grison" / "snapshots"
    snapshot_name = sorted(p.name for p in snapshots_dir.iterdir())[-1]
    ops_path = snapshots_dir / snapshot_name / "ops.json"
    ops = json.loads(ops_path.read_text(encoding="utf-8"))
    evidence_op = next(
        o for o in ops if o["kind"] == "gw.evidence" and o["outcome"] == "delete_remote"
    )
    evidence_op["remote_preimage"]["reportId"] = None
    ops_path.write_text(json.dumps(ops), encoding="utf-8")

    result = run_grison("undo")

    assert result.exit_code == 1, result.output
    assert "shot.png" in result.output
    assert gw_server.store.evidence == []  # never silently restored into a guessed scope


def test_one_reports_evidence_fileset_failure_does_not_abort_the_findings_phase(
    run_grison, gw_server, workspace, monkeypatch,
):
    """BRIEF task 2 (ENGINE.md §5 per-record isolation): before this fix, an
    unexpected exception out of one report's ``engine_sync_fileset`` call
    propagated straight out of ``_run_findings_phase`` — caught only at the
    WHOLE-PHASE level by ``grison.cli._run_phase`` ("findings sync failed: ..."),
    discarding the entire findings phase's result: library findings, and every
    OTHER report's findings, along with it. The fix wraps each report's evidence
    file-set sync in its own try/except, so one report's failure becomes that
    report's own `failed` record and every other report/finding still syncs."""
    import grison.cli as cli_mod

    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_report(id=8, title="Report B", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_finding(
        id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4,
        description="<p>old text</p>",
    )
    first = run_grison("sync")  # creates both report directories + pulls the library finding
    assert first.exit_code == 0, first.output

    lib_path = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    lib_path.write_text(
        lib_path.read_text(encoding="utf-8").replace("old text", "brand new text"),
        encoding="utf-8",
    )

    real_sync_fileset = cli_mod.engine_sync_fileset

    def _fake_sync_fileset(root, ctx, adapter, folder, **kwargs):
        if str(folder) == "findings/reports/report-b/evidence":
            raise RuntimeError("boom")
        return real_sync_fileset(root, ctx, adapter, folder, **kwargs)

    monkeypatch.setattr(cli_mod, "engine_sync_fileset", _fake_sync_fileset)

    result = run_grison("sync")

    # the phase-level "findings sync failed" message (grison.cli._run_phase) must
    # NEVER fire for this — the failure is report-b's evidence set's alone.
    assert "findings sync failed" not in result.output, result.output
    assert "gw.evidence[findings/reports/report-b]): failed 1" in result.output, result.output
    assert "boom" in result.output
    # everything that did NOT touch report-b's evidence still went through in the
    # SAME sync: the library finding push actually reached the fake server...
    assert "findings (gw.finding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.findings, 1)
    assert row is not None and "brand new text" in row["description"]
    # ...and report-a's own (clean) evidence set was not skipped either.
    assert "gw.evidence[findings/reports/report-a])" in result.output, result.output
    assert result.exit_code == 1, result.output  # still a problem — just correctly attributed


# --- identity: cross-report moves, copies, simultaneous renames, position -------


def test_cross_report_move_with_identical_content_reparents(run_grison, gw_server, workspace):
    """Bug fix: :meth:`GwReportedFindingAdapter.canonical_local`/``canonical_remote``
    now include report membership, the same way
    :meth:`~grison.adapters.bs_pages.BsPageAdapter.canonical_local`/``canonical_remote``
    include ``book``/``chapter`` — moving a finding FILE to another report
    directory with byte-identical content must still write the new ``reportId``
    to Ghostwriter, and a third sync must come back clean. BEFORE the fix,
    ``apply.py::_apply_move``'s ``needs_write`` compared the two canonical
    payloads equal (report membership was invisible to both), so the finding
    kept its OLD ``reportId`` on Ghostwriter forever, silently."""
    report_a = gw_server.store.seed_report(id=7, title="Report A",
                                           project={"scopes": REPORT_SCOPES})
    report_b = gw_server.store.seed_report(id=8, title="Report B",
                                           project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50, reportId=report_a["id"], title="Finding One", severityId=3, findingTypeId=4,
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    src = _rdir(workspace, "report-a") / "finding-one.md"
    dst = _rdir(workspace, "report-b") / "finding-one.md"
    dst.write_bytes(src.read_bytes())
    src.unlink()

    second = run_grison("sync")

    assert "move" in second.output, second.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row is not None and row["reportId"] == report_b["id"]
    index = Index.load(workspace)
    assert index.get("findings/reports/report-b/finding-one.md").id == 50
    assert index.get("findings/reports/report-a/finding-one.md") is None

    third = run_grison("sync")

    assert third.exit_code == 0, third.output
    assert "findings (gw.reportedFinding): clean" in third.output, third.output


def test_cross_report_move_and_edit_reparents(run_grison, gw_server, workspace):
    """Same fix, the MOVE+EDIT path: content changed AND the directory changed in
    the same sync — both the edit and the reparent must land in one write."""
    report_a = gw_server.store.seed_report(id=7, title="Report A",
                                           project={"scopes": REPORT_SCOPES})
    report_b = gw_server.store.seed_report(id=8, title="Report B",
                                           project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50, reportId=report_a["id"], title="Finding One", severityId=3, findingTypeId=4,
        description="<p>old text</p>",
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    src = _rdir(workspace, "report-a") / "finding-one.md"
    edited = src.read_text(encoding="utf-8").replace("old text", "brand new text")
    dst = _rdir(workspace, "report-b") / "finding-one.md"
    dst.write_text(edited, encoding="utf-8")
    src.unlink()

    result = run_grison("sync")

    assert "move" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row is not None
    assert row["reportId"] == report_b["id"]
    assert "brand new text" in row["description"]


def test_library_to_report_move_is_create_and_delete_never_a_pair(run_grison, gw_server, workspace):
    """D3/module docstring: a library<->report move is NOT a pairing move —
    ``gw.finding`` and ``gw.reportedFinding`` are different kinds/scan roots, so
    :mod:`grison.engine.identity` never even considers pairing across them. The
    file landing in ``findings/reports/<dir>/`` is a fresh CREATE and the old
    library path a DELETE_REMOTE — never a guessed move."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    src = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    dst = _rdir(workspace) / "weak-tls-ciphers.md"
    dst.write_bytes(src.read_bytes())
    src.unlink()

    result = run_grison("sync")

    assert "findings (gw.finding): delete_remote 1" in result.output, result.output
    assert "findings (gw.reportedFinding): create 1" in result.output, result.output
    assert gw_server.store._by_id(gw_server.store.findings, 1) is None
    assert len(gw_server.store.reported_findings) == 1
    assert gw_server.store.reported_findings[0]["reportId"] == report["id"]


def test_copying_a_finding_file_yields_a_second_record(run_grison, gw_server, workspace):
    """Copying (not moving) a finding file leaves the original path indexed and
    present — never an "M" — so the copy is unpaired and becomes CREATE: a
    second Ghostwriter record, never guessed as a move of the original."""
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    src = workspace / "findings" / "library" / "weak-tls-ciphers.md"
    dst = src.parent / "weak-tls-ciphers-copy.md"
    dst.write_bytes(src.read_bytes())

    result = run_grison("sync")

    assert "create 1" in result.output, result.output
    assert len(gw_server.store.findings) == 2


def test_two_simultaneous_renames_pair_one_to_one(run_grison, gw_server, workspace):
    """Two files renamed within the same report in one sync must each pair with
    their OWN record — :func:`grison.engine.identity.pair`'s one-to-one matching,
    never cross-paired just because both are "some M" and "some U" of the same
    kind."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
        description="<p>alpha</p>",
    )
    gw_server.store.seed_reported_finding(
        id=51, reportId=report["id"], title="Finding Two", severityId=3, findingTypeId=4,
        description="<p>beta</p>",
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    report_dir = _rdir(workspace)
    (report_dir / "finding-one.md").rename(report_dir / "renamed-one.md")
    (report_dir / "finding-two.md").rename(report_dir / "renamed-two.md")

    result = run_grison("sync")

    assert result.exit_code == 0, result.output
    index = Index.load(workspace)
    assert index.get("findings/reports/report-a/renamed-one.md").id == 50
    assert index.get("findings/reports/report-a/renamed-two.md").id == 51
    assert gw_server.store._by_id(gw_server.store.reported_findings, 50)["description"] == \
        "<p>alpha</p>"
    assert gw_server.store._by_id(gw_server.store.reported_findings, 51)["description"] == \
        "<p>beta</p>"


def test_position_preserved_across_plain_content_push(run_grison, gw_server, workspace):
    """``position`` is deliberately never in the update payload (module
    docstring) — a content-only push must not disturb the server's own position
    for the record."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50, reportId=report["id"], title="Finding One", severityId=3, findingTypeId=4,
        description="<p>old text</p>", position=7,
    )
    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    path = _rdir(workspace) / "finding-one.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("old text", "brand new text"),
        encoding="utf-8",
    )

    result = run_grison("sync")

    assert "gw.reportedFinding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row is not None and row["position"] == 7


# --- D1, verbatim: "replacing an image's bytes must re-push every finding
# referencing it" — automatically, no --force-local (coordinator correction over
# an earlier, weaker fix that only reached COLLISION; see grison.engine.filesets.
# canonical_remote_prose's docstring for the mechanism) ------------------------


def test_evidence_reupload_pushes_the_reported_finding_with_the_new_id(
    run_grison, gw_server, workspace,
):
    """Unlike a narrative section (reports phase, before findings phase), a
    reported finding's own evidence lives in the SAME phase as the reupload
    (grison.cli._run_findings_phase: each report's evidence/ fileset syncs,
    THEN evidence_by_report is rebuilt fresh, THEN the finding adapters run) —
    so the reupload and the finding's own re-push with the new id land in the
    SAME sync, no second sync needed."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90, reportId=7, document="evidence/7/shot.png", friendlyName="shot",
        caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=7, title="SQLi", severityId=5, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    run_grison("sync")
    finding_path = _rdir(workspace) / "sqli.md"
    original_text = finding_path.read_text(encoding="utf-8")
    assert "evidence/shot.png" in original_text

    evidence_file = _rdir(workspace) / "evidence" / "shot.png"
    evidence_file.write_bytes(b"brand new bytes, same filename")

    result = run_grison("sync")  # reupload AND re-push, same run

    assert "findings (gw.reportedFinding): push 1" in result.output, result.output
    assert "collision" not in result.output
    assert finding_path.read_text(encoding="utf-8") == original_text  # local text untouched
    new_id = next(row["id"] for row in gw_server.store.evidence if row["document"].endswith(
        "shot.png"
    ))
    assert new_id != 90
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert f'data-evidence-id="{new_id}"' in row["description"]

    before = len(gw_server.operation_log)
    clean_result = run_grison("sync")

    assert "findings (gw.reportedFinding): clean" in clean_result.output
    assert gw_server.operation_log[before:] == []  # settled


def test_evidence_reupload_pushes_every_finding_that_references_it(
    run_grison, gw_server, workspace,
):
    """One evidence file referenced by TWO reported findings: a reupload must
    re-push BOTH, each with the SAME new id."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90, reportId=7, document="evidence/7/shot.png", friendlyName="shot",
        caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=7, title="SQLi", severityId=5, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    gw_server.store.seed_reported_finding(
        id=51, reportId=7, title="XSS", severityId=4, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    run_grison("sync")

    evidence_file = _rdir(workspace) / "evidence" / "shot.png"
    evidence_file.write_bytes(b"brand new bytes, same filename")

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): push 2" in result.output, result.output
    new_id = next(row["id"] for row in gw_server.store.evidence if row["document"].endswith(
        "shot.png"
    ))
    row50 = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    row51 = gw_server.store._by_id(gw_server.store.reported_findings, 51)
    assert f'data-evidence-id="{new_id}"' in row50["description"]
    assert f'data-evidence-id="{new_id}"' in row51["description"]


def test_evidence_reupload_plus_a_concurrent_remote_edit_is_still_a_collision(
    run_grison, gw_server, workspace,
):
    """The pre-write re-fetch guard must still catch a GENUINE concurrent
    edit — the reupload's automatic re-push must never blindly overwrite a
    finding someone else changed on Ghostwriter in between."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90, reportId=7, document="evidence/7/shot.png", friendlyName="shot",
        caption="Login screen",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=7, title="SQLi", severityId=5, findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    run_grison("sync")

    evidence_file = _rdir(workspace) / "evidence" / "shot.png"
    evidence_file.write_bytes(b"brand new bytes, same filename")

    def concurrent_edit() -> None:
        row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
        row["description"] = "<p>someone changed this on Ghostwriter</p>"

    baseline = gw_server.call_count("reportedFinding_by_pk")
    gw_server.on_request("reportedFinding_by_pk", concurrent_edit, call_number=baseline + 1)

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): collision 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row["description"] == "<p>someone changed this on Ghostwriter</p>"  # push withheld


def test_non_ascii_evidence_filename_pushes_from_a_reported_finding(
    run_grison, gw_server, workspace,
):
    """grison.markdown.refscan item 3's fix also applies to GwRefResolver
    (grison/adapters/gw_findings.py) — markdown-it-py percent-encodes non-ASCII
    bytes in an image destination on the converter's own parse, so
    GwRefResolver.to_remote/_id_for_path used to fail to resolve
    evidence/Phishing_Sonuçları.png against the index (keyed by the real,
    decoded path) and raise ConverterError instead of pushing."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90, reportId=7, document="evidence/7/Phishing_Sonuçları.png",
        friendlyName="Phishing_Sonuçları",
    )
    gw_server.store.seed_reported_finding(
        id=50, reportId=7, title="Phishing", severityId=3, findingTypeId=4,
    )
    run_grison("sync")  # downloads + indexes evidence/Phishing_Sonuçları.png

    finding_path = _rdir(workspace) / "phishing.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\n",
        "## Description\n\n![Results](evidence/Phishing_Sonuçları.png)\n\n",
        1,
    )
    finding_path.write_text(text, encoding="utf-8")

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert 'data-evidence-id="90"' in row["description"]
