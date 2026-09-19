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


def test_non_ascii_uppercase_evidence_name_validates_and_syncs(run_grison, gw_server, workspace):
    """Item 2 (engine-findings-lab.md 'Scenario 6'): the exact motivating case —
    ``WS-001``'s charset rule no longer applies to a file's own name directly
    inside ``evidence/`` (``REF-008`` — see ``tests/test_validator_ref.py`` for
    the offline validate-side proof — has no charset opinion at all), so a real
    non-ASCII, mixed-case evidence name both validates cleanly AND uploads,
    where it used to fail `grison validate` outright even though `grison sync`
    uploaded it anyway (the mismatch the "withheld by sync" half of this fix
    resolves — see ``tests/test_engine_filesets.py``'s
    ``test_invalid_fileset_create_is_withheld_others_proceed`` for that half,
    proven at the engine level since a REF-008 failure mode that can still
    reach a real local-scan CREATE candidate can't be created on a normal
    filesystem via a shell/test — see that test's own docstring)."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
    )
    run_grison("sync")

    finding_path = report_dir / "finding-one.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\n",
        "## Description\n\n![A screenshot](evidence/Sonuçları_ş.png)\n\n",
        1,
    )
    finding_path.write_text(text, encoding="utf-8")
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "Sonuçları_ş.png").write_bytes(b"\x89PNG-fake-bytes")

    result = run_grison("sync")

    assert "create" in result.output and "invalid" not in result.output, result.output
    assert len(gw_server.store.evidence) == 1
    row = gw_server.store.evidence[0]
    assert row["friendlyName"] == "Sonuçları_ş"
    updated_finding = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert updated_finding is not None
    assert f'data-evidence-id="{row["id"]}"' in updated_finding["description"]


def test_reported_finding_cross_reference_and_embed_converge_after_one_push(
    run_grison,
    gw_server,
    workspace,
):
    """Item 1 (engine-findings-lab.md 'Two defects'/#2): a plain cross-reference
    link (``[caption](evidence/file "desc")``, no ``!``) used to never
    converge — ``canonical_prose`` (LOCAL) left it completely untouched while
    ``canonical_remote_prose`` (REMOTE, via ``html_to_md`` on the pushed
    ``<span data-gw-ref-encoded>``) always rendered it as a literal
    ``[<id>](<id>)`` — a permanent push loop, confirmed in the lab across four
    consecutive no-op syncs. Both sides now fold the same identity token for
    both an embed and a cross-reference, so one push settles it for good."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="SQL Injection",
        severityId=5,
        findingTypeId=4,
    )
    first = run_grison("sync")
    assert "findings (gw.reportedFinding): pull_new 1" in first.output, first.output

    finding_path = report_dir / "sql-injection.md"
    text = finding_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\n",
        "## Description\n\n![Login screenshot](evidence/login_page.png)\n\n"
        "See [DB dump](evidence/db_dump.png) for the resulting database extraction.\n\n",
        1,
    )
    finding_path.write_text(text, encoding="utf-8")
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "login_page.png").write_bytes(b"\x89PNG-fake-1")
    (report_dir / "evidence" / "db_dump.png").write_bytes(b"\x89PNG-fake-2")

    pushed = run_grison("sync")
    assert "findings (gw.reportedFinding): push 1" in pushed.output, pushed.output
    assert len(gw_server.store.evidence) == 2
    updated = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert updated is not None
    assert "richtext-evidence" in updated["description"]  # the embed
    assert "data-gw-ref-encoded" in updated["description"]  # the cross-reference

    clean1 = run_grison("sync")
    assert "findings (gw.reportedFinding): clean 1" in clean1.output, clean1.output
    clean2 = run_grison("sync")
    assert "findings (gw.reportedFinding): clean 1" in clean2.output, clean2.output


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


def test_undo_of_an_evidence_create_plus_two_finding_pushes_restores_both_files(
    run_grison,
    gw_server,
    workspace,
):
    """Item 3 (engine-findings-lab.md Scenario 12): one ``grison sync`` that both
    uploads a new evidence file and pushes two reported findings (one of them
    referencing the new evidence) used to print "restored pre-undo content" for
    BOTH pushed findings after ``grison undo``, but only ever rewrote the LOCAL
    file for ONE of them — the other kept its post-push content (including the
    now-dangling evidence reference), producing a spurious collision on the
    very next sync. Fixed: every push/move_edit op's own local pre-image is
    restored (``grison.engine.apply``'s ``UndoOp.local_preimage``, previously
    only ever captured for a ``create`` op), and the "restored" message is only
    printed once that write has actually happened."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Default Credentials",
        severityId=3,
        findingTypeId=4,
    )
    gw_server.store.seed_reported_finding(
        id=51,
        reportId=report["id"],
        title="Unencrypted Telnet",
        severityId=4,
        findingTypeId=4,
    )
    run_grison("sync")

    default_creds_path = report_dir / "default-credentials.md"
    telnet_path = report_dir / "unencrypted-telnet.md"
    default_creds_baseline = default_creds_path.read_text(encoding="utf-8")
    telnet_baseline = telnet_path.read_text(encoding="utf-8")

    default_creds_path.write_text(
        default_creds_path.read_text(encoding="utf-8").replace(
            "## Description\n\n",
            "## Description\n\nEdited by the undo-scenario test.\n\n",
            1,
        ),
        encoding="utf-8",
    )
    telnet_path.write_text(
        telnet_path.read_text(encoding="utf-8").replace(
            "## Description\n\n",
            "## Description\n\n![Undo test screenshot](evidence/undo_test.png)\n\n",
            1,
        ),
        encoding="utf-8",
    )
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "undo_test.png").write_bytes(b"\x89PNG-undo-test")

    pushed = run_grison("sync")
    assert "findings (gw.reportedFinding): push 2" in pushed.output, pushed.output
    assert len(gw_server.store.evidence) == 1
    assert default_creds_path.read_text(encoding="utf-8") != default_creds_baseline
    assert telnet_path.read_text(encoding="utf-8") != telnet_baseline

    undone = run_grison("undo")

    assert undone.exit_code == 0, undone.output
    assert default_creds_path.read_text(encoding="utf-8") == default_creds_baseline
    assert telnet_path.read_text(encoding="utf-8") == telnet_baseline  # no dangling embed
    assert gw_server.store.evidence == []
    assert not (report_dir / "evidence" / "undo_test.png").exists()

    clean = run_grison("sync")
    assert "findings (gw.reportedFinding): clean 2" in clean.output, clean.output
    assert "collision" not in clean.output, clean.output


def test_undo_with_a_preexisting_cross_reference_is_fully_clean_afterward(
    run_grison,
    gw_server,
    workspace,
):
    """Item 3 (fix-undo-repair) root cause: ``grison undo``'s own
    ``GwReportedFindingAdapter`` (``grison.cli.undo``) used to be built with
    ``evidence_by_report={}`` — every REAL sync (``_run_reports_phase``/
    ``_run_findings_phase``) builds this dict for real and passes it to the same
    adapter class; ``undo`` was the one caller that hard-coded it empty.
    ``canonical_remote()`` needs it to resolve a plain CROSS-REFERENCE (``[caption]
    (evidence/x.png)`` — resolved by NAME on the wire, unlike an embed's native
    ``data-evidence-id="N"`` which resolves by id via the index alone and was
    never affected) through :class:`~grison.engine.filesets._LiteralEmbedResolver`;
    an empty dict resolves that name to a placeholder instead of the real id, so
    undo's own restamp (and its pre-write re-fetch drift guard, which ALSO calls
    ``canonical_remote()``) can never agree with what the next real sync computes.
    BEFORE the fix this surfaced as ``grison undo`` itself refusing to restore
    ("changed since the sync that wrote it — not restoring") for the finding
    holding the cross-reference, exit code 1 — the lab's own run (a real
    Ghostwriter) hit the milder sibling of the same bug, a one-off benign
    ``repair`` on the very next sync instead of an outright refusal, depending on
    which of undo's two ``canonical_remote()`` call sites tripped first. Fixed:
    ``grison.cli.undo`` now builds the real ``evidence_by_report`` the same way
    the forward phases do."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    report_dir = _rdir(workspace)
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Default Credentials",
        severityId=3,
        findingTypeId=4,
    )
    gw_server.store.seed_reported_finding(
        id=51,
        reportId=report["id"],
        title="Unencrypted Telnet",
        severityId=4,
        findingTypeId=4,
    )
    run_grison("sync")

    default_creds_path = report_dir / "default-credentials.md"
    telnet_path = report_dir / "unencrypted-telnet.md"

    # A PRE-EXISTING cross-reference (plain link, never an embed) to an evidence
    # file established in an EARLIER, separate sync — not part of the snapshot
    # under test below.
    default_creds_path.write_text(
        default_creds_path.read_text(encoding="utf-8").replace(
            "## Description\n\n",
            "## Description\n\nSee [existing](evidence/existing.png) for context.\n\n",
            1,
        ),
        encoding="utf-8",
    )
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (report_dir / "evidence" / "existing.png").write_bytes(b"\x89PNG-existing")
    run_grison("sync")

    default_creds_baseline = default_creds_path.read_text(encoding="utf-8")
    telnet_baseline = telnet_path.read_text(encoding="utf-8")

    # THIS is the snapshot under test: two unrelated text edits (neither touches
    # the cross-reference itself) plus a brand-new, unrelated evidence create —
    # matching the task's exact scenario ("one evidence create + two finding
    # pushes").
    default_creds_path.write_text(
        default_creds_path.read_text(encoding="utf-8").replace(
            "## Description\n\n",
            "## Description\n\nEdited by the undo-scenario test.\n\n",
            1,
        ),
        encoding="utf-8",
    )
    telnet_path.write_text(
        telnet_path.read_text(encoding="utf-8").replace(
            "## Description\n\n",
            "## Description\n\nAlso edited.\n\n",
            1,
        ),
        encoding="utf-8",
    )
    (report_dir / "evidence" / "new.png").write_bytes(b"\x89PNG-new")

    pushed = run_grison("sync")
    assert "findings (gw.reportedFinding): push 2" in pushed.output, pushed.output
    assert len(gw_server.store.evidence) == 2

    undone = run_grison("undo")
    assert undone.exit_code == 0, undone.output  # BEFORE the fix: "not restoring", exit 1
    assert "failed" not in undone.output, undone.output
    assert default_creds_path.read_text(encoding="utf-8") == default_creds_baseline
    assert telnet_path.read_text(encoding="utf-8") == telnet_baseline
    # the evidence create's own undo must also clear its file-set state entry —
    # only "existing.png"'s (untouched by this snapshot) should remain.
    evidence_state_dir = workspace / ".grison" / "state" / "gw.evidence"
    remaining_state_ids = {p.stem for p in evidence_state_dir.iterdir()}
    assert remaining_state_ids == {str(gw_server.store.evidence[0]["id"])}
    assert len(gw_server.store.evidence) == 1

    clean = run_grison("sync")
    assert clean.exit_code == 0, clean.output
    assert "repair" not in clean.output, clean.output  # BEFORE the fix: a one-off repair here
    assert "findings (gw.reportedFinding): clean 2" in clean.output, clean.output


def test_n_new_evidence_uploads_fetch_the_org_wide_evidence_list_only_once(
    run_grison,
    gw_server,
    workspace,
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
    run_grison,
    gw_server,
    workspace,
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
    run_grison,
    gw_server,
    workspace,
    monkeypatch,
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
        id=1,
        title="Weak TLS Ciphers",
        severityId=3,
        findingTypeId=4,
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
    report_a = gw_server.store.seed_report(
        id=7, title="Report A", project={"scopes": REPORT_SCOPES}
    )
    report_b = gw_server.store.seed_report(
        id=8, title="Report B", project={"scopes": REPORT_SCOPES}
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report_a["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
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
    report_a = gw_server.store.seed_report(
        id=7, title="Report A", project={"scopes": REPORT_SCOPES}
    )
    report_b = gw_server.store.seed_report(
        id=8, title="Report B", project={"scopes": REPORT_SCOPES}
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report_a["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
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
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
        description="<p>alpha</p>",
    )
    gw_server.store.seed_reported_finding(
        id=51,
        reportId=report["id"],
        title="Finding Two",
        severityId=3,
        findingTypeId=4,
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
    assert (
        gw_server.store._by_id(gw_server.store.reported_findings, 50)["description"]
        == "<p>alpha</p>"
    )
    assert (
        gw_server.store._by_id(gw_server.store.reported_findings, 51)["description"]
        == "<p>beta</p>"
    )


def test_position_preserved_across_plain_content_push(run_grison, gw_server, workspace):
    """``position`` is deliberately never in the update payload (module
    docstring) — a content-only push must not disturb the server's own position
    for the record."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
        description="<p>old text</p>",
        position=7,
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
    run_grison,
    gw_server,
    workspace,
):
    """Unlike a narrative section (reports phase, before findings phase), a
    reported finding's own evidence lives in the SAME phase as the reupload
    (grison.cli._run_findings_phase: each report's evidence/ fileset syncs,
    THEN evidence_by_report is rebuilt fresh, THEN the finding adapters run) —
    so the reupload and the finding's own re-push with the new id land in the
    SAME sync, no second sync needed."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
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
    new_id = next(
        row["id"] for row in gw_server.store.evidence if row["document"].endswith("shot.png")
    )
    assert new_id != 90
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert f'data-evidence-id="{new_id}"' in row["description"]

    before = len(gw_server.operation_log)
    clean_result = run_grison("sync")

    assert "findings (gw.reportedFinding): clean" in clean_result.output
    assert gw_server.operation_log[before:] == []  # settled


def test_evidence_reupload_pushes_every_finding_that_references_it(
    run_grison,
    gw_server,
    workspace,
):
    """One evidence file referenced by TWO reported findings: a reupload must
    re-push BOTH, each with the SAME new id."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
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
    gw_server.store.seed_reported_finding(
        id=51,
        reportId=7,
        title="XSS",
        severityId=4,
        findingTypeId=4,
        description='<div class="richtext-evidence" data-evidence-id="90"></div>',
    )
    run_grison("sync")

    evidence_file = _rdir(workspace) / "evidence" / "shot.png"
    evidence_file.write_bytes(b"brand new bytes, same filename")

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): push 2" in result.output, result.output
    new_id = next(
        row["id"] for row in gw_server.store.evidence if row["document"].endswith("shot.png")
    )
    row50 = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    row51 = gw_server.store._by_id(gw_server.store.reported_findings, 51)
    assert f'data-evidence-id="{new_id}"' in row50["description"]
    assert f'data-evidence-id="{new_id}"' in row51["description"]


def test_evidence_reupload_plus_a_concurrent_remote_edit_is_still_a_collision(
    run_grison,
    gw_server,
    workspace,
):
    """The pre-write re-fetch guard must still catch a GENUINE concurrent
    edit — the reupload's automatic re-push must never blindly overwrite a
    finding someone else changed on Ghostwriter in between."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
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
    run_grison,
    gw_server,
    workspace,
):
    """grison.markdown.refscan item 3's fix also applies to GwRefResolver
    (grison/adapters/gw_findings.py) — markdown-it-py percent-encodes non-ASCII
    bytes in an image destination on the converter's own parse, so
    GwRefResolver.to_remote/_id_for_path used to fail to resolve
    evidence/Phishing_Sonuçları.png against the index (keyed by the real,
    decoded path) and raise ConverterError instead of pushing."""
    gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/Phishing_Sonuçları.png",
        friendlyName="Phishing_Sonuçları",
    )
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=7,
        title="Phishing",
        severityId=3,
        findingTypeId=4,
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


# --- lab-discovered defects (engine-findings-lab.md scenarios 1 and 13) -----------
# Real Ghostwriter 7.2.6 seed data hit three converter bugs on a totally fresh
# sync — reproduced here with the exact seed HTML that triggered each one.


def test_library_finding_with_a_real_editor_heading_pulls_and_stays_clean(
    run_grison,
    gw_server,
    workspace,
):
    """Defect 1: a real TipTap 7.2.6 editor writes h1-h6 into a plain FINDING
    field, not just report narrative — the exact stored HTML from
    ``tests/fixtures/lab-samples/gw-findings.json``'s library finding 3
    (title "Default Credentials on Admin Panel" in the real lab). Before the
    fix this raised ``ConverterError: unsupported HTML tag: <h3>`` on every
    single sync, forever (engine-findings-lab.md, "Two defects", #1)."""
    gw_server.store.seed_finding(
        id=3,
        title="Default Credentials on Admin Panel",
        severityId=3,
        findingTypeId=1,
        description=(
            "<h3>Overview</h3><p>The admin panel at <code>/admin</code> accepts "
            "the vendor default credentials <code>admin:admin</code>.</p>"
        ),
    )

    first = run_grison("sync")

    assert first.exit_code == 0, first.output
    assert "findings (gw.finding): pull_new 1" in first.output, first.output
    path = workspace / "findings" / "library" / "default-credentials-on-admin-panel.md"
    text = path.read_text(encoding="utf-8")
    assert "### Overview" in text
    assert "The admin panel at `/admin`" in text

    second = run_grison("sync")  # a fresh pull must not need a settle push

    assert "findings (gw.finding): clean" in second.output, second.output
    assert "push" not in second.output, second.output


def test_library_finding_with_external_link_stays_clean_across_syncs(
    run_grison,
    gw_server,
    workspace,
):
    """fix-f item 1 (+ coordinator addendum): ``_substitute_ref_identity`` used
    to fold ANY ``[text](dest)`` it found — including a plain external
    citation link with no ``evidence/`` prefix at all — into
    ``[unresolved](unresolved)`` on the LOCAL canonical side, while the REMOTE
    side (the real converter's ordinary ``<a>`` rendering) never touches an
    external link at all. The two canonical forms then permanently disagreed,
    classifying the record PUSH forever with nothing genuinely changed. Only a
    reference ``_is_grison_reference`` recognises (a destination starting with
    ``evidence/``) is folded now; an external link is left byte-identical on
    both sides, so a second and third sync (nothing edited in between) both
    come back CLEAN."""
    gw_server.store.seed_finding(
        id=1,
        title="SQL Injection",
        severityId=3,
        findingTypeId=4,
        description=(
            '<p>See <a href="https://owasp.org/www-community/attacks/SQL_Injection" '
            'target="_blank" rel="noopener">OWASP: SQL Injection</a> for background.</p>'
        ),
    )

    first = run_grison("sync")
    assert first.exit_code == 0, first.output
    assert "findings (gw.finding): pull_new 1" in first.output, first.output

    second = run_grison("sync")
    assert "findings (gw.finding): clean" in second.output, second.output
    assert "push" not in second.output, second.output

    third = run_grison("sync")
    assert "findings (gw.finding): clean" in third.output, third.output
    assert "push" not in third.output, third.output


def test_reported_finding_heading_edit_pushes_back_as_html(run_grison, gw_server, workspace):
    """The push direction of defect 1: an author editing a section that already
    has (or gains) a markdown heading must push back as real ``<hN>`` HTML, not
    fail ``md_to_html`` the way it did before the finding adapters passed
    ``headings=True`` (see ``grison.markdown.converter``'s module docstring)."""
    report = gw_server.store.seed_report(id=7, title="Report A", project={"scopes": REPORT_SCOPES})
    gw_server.store.seed_reported_finding(
        id=50,
        reportId=report["id"],
        title="Finding One",
        severityId=3,
        findingTypeId=4,
        description="<p>plain text</p>",
    )
    run_grison("sync")

    path = _rdir(workspace) / "finding-one.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\nplain text",
        "## Description\n\n### Overview\n\nplain text",
        1,
    )
    path.write_text(text, encoding="utf-8")

    result = run_grison("sync")

    assert "findings (gw.reportedFinding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.reported_findings, 50)
    assert row["description"] == "<h3>Overview</h3>\n\n<p>plain text</p>"


def test_raw_wrapped_template_literal_pulls_as_plain_text_not_gw_markers(
    run_grison,
    gw_server,
    workspace,
):
    """Defect 2: existing stored HTML with a real Jinja ``{% raw %}...{% endraw %}``
    block (from an older grison push, or a human typing it directly — the exact
    seed content from engine-findings-lab.md's "Template Injection Payload
    Reference" finding). Before the fix ``html_to_md`` produced malformed markdown
    with mismatched backtick fences, treating every token inside the raw block as
    an independently-active Jinja expression; the fix resolves the whole raw
    region to its plain literal text (D10: a raw block means "never evaluate
    this"), with no ``gw:`` code spans and no raw markers left in the markdown."""
    gw_server.store.seed_finding(
        id=12,
        title="Template Injection Payload Reference",
        severityId=2,
        findingTypeId=1,
        description="<p>{% raw %}{{7*7}} and {% debug %}{% endraw %}</p>",
    )

    first = run_grison("sync")

    assert first.exit_code == 0, first.output
    path = workspace / "findings" / "library" / "template-injection-payload-reference.md"
    text = path.read_text(encoding="utf-8")
    assert "{{7*7}} and {% debug %}" in text
    assert "gw:" not in text
    assert "{% raw %}" not in text
    assert "{% endraw %}" not in text
    assert "`" not in text  # no code-span fencing was needed at all

    second = run_grison("sync")  # a fresh pull must not need a settle push

    assert "findings (gw.finding): clean" in second.output, second.output
    assert "push" not in second.output, second.output

    # Force a re-push (unrelated field edit) and confirm the re-pushed HTML uses
    # D10's ONE proven mechanism (per-token Jinja string-literal escaping) with
    # no nesting — never the old, broken "quoting inside a raw block" shape.
    path.write_text(
        text.replace("## Impact", "## Impact\n\nedited", 1),
        encoding="utf-8",
    )
    result = run_grison("sync")
    assert "findings (gw.finding): push 1" in result.output, result.output
    row = gw_server.store._by_id(gw_server.store.findings, 12)
    assert row["description"] == ("<p>{{ '{{' }}7*7{{ '}}' }} and {{ '{%' }} debug {{ '%}' }}</p>")
    assert "{% raw %}" not in row["description"]  # never re-emitted; never nested inside one


def test_nbsp_padded_trailing_paragraphs_pull_clean_with_no_settle_push(
    run_grison,
    gw_server,
    workspace,
):
    """Defect 3: the exact stored HTML of the real lab's "Missing HTTP Security
    Headers" library finding (id 4) — a real body paragraph followed by a
    genuinely empty ``<p></p>`` and a ``<p>&nbsp;&nbsp;&nbsp;</p>`` (TipTap's own
    editor leaves these behind routinely). Before the fix, canonical_local(pulled
    file) != canonical_remote(html) for this record: ``html_to_md`` kept the
    ``&nbsp;`` run as visible content and left phantom blank blocks in the join,
    neither of which survives ``md_to_html``'s real CommonMark reparse — so a
    totally fresh, untouched pull needed exactly one spurious settle push
    (engine-findings-lab.md, Scenario 1's "second sync not clean")."""
    gw_server.store.seed_finding(
        id=4,
        title="Missing HTTP Security Headers",
        severityId=3,
        findingTypeId=1,
        description=(
            "<p>The application does not set <code>Content-Security-Policy</code>, "
            "<code>X-Content-Type-Options</code>, or "
            "<code>Strict-Transport-Security</code>.</p><p></p>"
            "<p>&nbsp;&nbsp;&nbsp;</p>"
        ),
        impact="<p>Increases exposure to XSS and MIME-sniffing attacks.</p>",
        mitigation="<p>Add the missing headers at the reverse proxy layer.</p>",
        replication_steps="<p>Inspect response headers with <code>curl -I</code>.</p>",
    )

    first = run_grison("sync")
    assert first.exit_code == 0, first.output

    second = run_grison("sync")  # a fresh, untouched pull must never need a push

    assert "findings (gw.finding): clean" in second.output, second.output
    assert "push" not in second.output, second.output
