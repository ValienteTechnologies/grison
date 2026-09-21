"""End-to-end report-narrative + project-note sync scenarios — CLI + on-disk files +
``gw_server``'s inspection API only. No `grison:` blocks anywhere, index-backed
identity throughout (D3), mirrors with digest hand-edit detection, narrative sections
driven by the report's ``extraFieldSpec`` rows (not guessed from ``extraFields``
keys).

Every ``run_grison("sync")`` also runs the findings phase (engine-managed now —
see ``tests/e2e/test_findings_e2e.py``), which is clean by default whenever a
scenario here doesn't seed any library/reported findings itself. These tests
assert the reports phase's own summary line, the Ghostwriter operation log, and
the files on disk, rather than assuming anything about the overall exit code.

Tests changed on purpose (rewritten wholesale for the engine step; see each
docstring for the specific reasoning):

- Every directory-naming assertion drops the old v1 ``<id>-<slug>`` prefix — a
  freshly-pulled report directory is ``slug(title)`` with no numeric prefix at all
  (D3/D4); a mirrored note file is ``slug(words).md`` with no ``<id>-`` prefix
  either (names are stable handles, never derived-then-discarded).
- Every "reports: pull N, push M (...)" combined summary line becomes the engine's
  own per-kind "reports (gw.reportSection): ..."/"reports (gw.projectNote): ..."
  counts lines (ENGINE.md's one events/result policy, shared with the wiki phase).
- A report's narrative sections are now driven by ``extraFieldSpec`` (position,
  ``internalName``), not guessed from whichever keys happen to be populated in
  ``extraFields`` — every spec field always gets its own section file, even one
  with empty content. This changes exact pull counts everywhere: tests use
  ``_use_fields`` to pin the field set to just what a given scenario cares about,
  instead of asserting against a blanket "1 section pulled".
- ``test_section_removed_remotely_kept_locally_persists_across_two_syncs`` and
  ``test_removed_remotely_marker_clears_once_the_local_file_is_deleted`` are
  DROPPED: they tested repmap.py's old "guess sections from extraFields keys"
  behavior, where deleting a *value* from the ``extraFields`` map made a section
  vanish from the sync entirely. Under the new design a section's existence is
  the ``extraFieldSpec`` row, not whether ``extraFields`` happens to have that key
  — deleting the value now just means "empty content" (see
  ``test_extra_fields_value_deleted_still_pulls_as_empty``), and the real "a
  section disappears" case is the spec row itself being retired (see
  ``test_new_section_appears_remotely_on_second_sync`` for the mirror-image case:
  a spec row being added).
- ``test_force_local_on_a_never_synced_hand_created_section`` is DROPPED: it
  hand-authored a narrative file under the OLD ``<id>-<slug>`` directory name
  *before* the report was ever pulled. Under the new naming a report directory
  doesn't exist locally until grison creates it (D3: "never created locally"),
  so the scenario cannot arise the same way; the nearest equivalent (a local
  file for a spec'd field created in the SAME sync that first creates the
  report directory) is a narrow, accepted bootstrapping-order edge case — see
  ``grison.adapters.gw_report``'s module docstring and the final report's "forks"
  section — not worth a dedicated regression test.
- ``test_section_deleted_locally_is_silently_repulled`` is REPLACED by
  ``test_deleting_an_unedited_local_section_clears_it_remotely``: D6 ("one
  classification table" for every kind sharing the engine) makes "local file
  deleted, unmodified relative to the last sync" a DELETE_REMOTE for every
  read-write kind, sections included — narrative's own bespoke "always silently
  repull, never delete" behavior doesn't survive moving onto the shared engine.
  A section has no real "delete" in Ghostwriter, so DELETE_REMOTE clears the
  field's content back to empty (see ``NarrativeSectionAdapter.delete``).
- ``test_project_md_hand_edit_is_silently_overwritten_today`` and
  ``test_report_yml_hand_edit_is_silently_overwritten_today`` are DROPPED, and
  their strict-xfail twins (``..._is_rejected_by_validation_once_decided``) are
  PROMOTED to real, un-xfailed tests: both mirrors now go through the shared
  ``grison.engine.mirrors.write_mirror_guarded`` digest guard (the same one
  ``bs_structure.py`` already used for book/chapter mirrors) — a hand-edit is
  detected and left alone, not silently clobbered, and ``grison validate``'s
  WS-009 independently flags it.
- ``test_existing_note_mirror_is_never_updated_when_the_remote_note_is_edited`` is
  REPLACED by ``test_mirrored_note_edited_remotely_is_pulled``: coordinator
  correction — ENGINE.md's read-only-kinds rule ("only ever take PULL / PULL_NEW /
  DELETE_LOCAL") applies to append-only's already-indexed half too; a mirrored
  note DOES pull a remote edit at the SAME path (never a second, differently-named
  file — that old filename was an accidental side effect of v1 deriving it from
  body text). ``grison/engine/classify.py``'s append-only branch now delegates to
  the ordinary indexed table + read-only clamp instead of hardcoding CLEAN.
- ``test_pull_surfaces_a_dropped_styling_construct_as_a_warning_once`` is back
  (coordinator correction), renamed
  ``test_pull_surfaces_a_dropped_styling_construct_as_a_loss_event_once``, on top
  of the new engine-wide ``loss`` event verb (``grison.engine.events.emit_losses``)
  — every adapter that converts HTML to local text on pull/create/push-then-
  recanonicalize gets this for free from the apply loop now, not a per-kind
  bespoke warning list.
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


def _use_fields(gw_server, *names: str) -> None:
    """Pin this fake's report ``extraFieldSpec`` rows to exactly ``names`` (instead
    of the 7 lab-shaped defaults ``seed_defaults()`` seeds) — keeps a scenario's
    pull/push counts crisp and legible instead of every test having to account for
    all 7 fields."""
    gw_server.store.extra_field_specs.clear()
    gw_server.store.seed_report_extra_field_specs(names)


def _rdir(slug: str) -> Path:
    return Path.cwd() / "findings" / "reports" / slug


def test_first_sync_creates_narrative_project_and_notes(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary text.</p>"},
        project={
            "codename": "OP-A",
            "scopes": REPORT_SCOPES,
            "comments": [
                {
                    "id": 1,
                    "note": "<p>Kickoff note.</p>",
                    "timestamp": "2026-01-02",
                    "operatorId": 1,
                    "user": {"name": "Lab Admin", "username": "lab"},
                }
            ],
        },
    )

    result = run_grison("sync")

    assert "reports: create 1 report dir(s)" in result.output
    assert "reports (gw.reportSection): pull_new 1" in result.output
    assert "reports (gw.projectNote): pull_new 1" in result.output
    rdir = _rdir("report-a")
    assert (rdir / "narrative" / "executive_summary.md").read_text(
        encoding="utf-8"
    ).strip() == "Summary text."
    assert (rdir / ".report.yml").exists()
    assert "narrative_order:" in (rdir / ".report.yml").read_text(encoding="utf-8")
    project_md = (rdir / "project.md").read_text(encoding="utf-8")
    assert "OP-A" in project_md
    assert "Internal range" in project_md
    notes = list((rdir / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "Kickoff note." in notes[0].read_text(encoding="utf-8")
    assert "author: Lab Admin" in notes[0].read_text(encoding="utf-8")
    assert gw_server.operation_log == []  # a pull never mutates Ghostwriter


def test_second_sync_is_a_noop(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    before = section.read_text(encoding="utf-8")

    result = run_grison("sync")

    assert "reports (gw.reportSection): clean 1" in result.output
    assert section.read_text(encoding="utf-8") == before
    assert gw_server.operation_log == []


def test_local_edit_pushes_one_section(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.write_text("Edited by hand.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "reports (gw.reportSection): push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "Edited by hand." in report["extraFields"]["executive_summary"]
    names = [o.name for o in gw_server.operation_log]
    assert names == ["update_report_by_pk"]


def test_remote_edit_pulls_one_section(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Changed on Ghostwriter.</p>"

    result = run_grison("sync")

    assert "reports (gw.reportSection): pull 1" in result.output
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    assert section.read_text(encoding="utf-8").strip() == "Changed on Ghostwriter."
    assert gw_server.operation_log == []


def test_dry_run_writes_nothing_and_reports_both_directions(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary", "methodology")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>", "methodology": "<p>Approach.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    exec_section = _rdir("report-a") / "narrative" / "executive_summary.md"
    meth_section = _rdir("report-a") / "narrative" / "methodology.md"
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
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
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
    assert "reports (gw.reportSection): push 1" in result.output
    assert "Local change." in report["extraFields"]["executive_summary"]
    assert not sidecar.exists()  # cleared once resolved (ENGINE.md §8)


def test_force_remote_resolves_collision(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.write_text("Local change.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Remote change.</p>"
    run_grison("sync")  # surfaces the collision + sidecar

    result = run_grison("sync", "--force-remote", str(section))

    assert "reports (gw.reportSection): pull 1" in result.output
    assert section.read_text(encoding="utf-8").strip() == "Remote change."


def test_new_local_note_is_pushed(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    notes_dir = _rdir("report-a") / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "team-note.md").write_text("A note for the team.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "reports (gw.projectNote): create 1" in result.output
    assert len(gw_server.store.project_notes) == 1
    assert "A note for the team." in gw_server.store.project_notes[0]["note"]
    # tests changed on purpose (D3): the mirrored replacement lands at the SAME
    # path — names are stable handles, no "<id>-slug.md" rename.
    assert (notes_dir / "team-note.md").exists()
    assert "author:" in (notes_dir / "team-note.md").read_text(encoding="utf-8")
    assert len(list(notes_dir.glob("*.md"))) == 1


def test_note_push_fails_loudly_when_operator_cannot_be_resolved(run_grison, gw_server):
    """If Ghostwriter's ``whoami`` names a user this token's ``user`` lookup can't
    find, the note push fails loudly (a FAILED outcome, per-record isolated) instead
    of guessing an operator id."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.users = []  # whoami's username now resolves to nobody
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    notes_dir = _rdir("report-a") / "notes"

    run_grison("sync")  # first sync: creates the dir + narrative, notes dir doesn't exist yet
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "team-note.md").write_text("A note.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "could not resolve ghostwriter user id" in result.output.lower()
    assert gw_server.store.project_notes == []


def test_missing_scope_is_a_lint_not_a_block(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"codename": "OP-NOSCOPE", "scopes": []},
    )

    result = run_grison("sync")

    assert "report 7 (OP-NOSCOPE): project has no scope defined" in result.output
    assert "reports (gw.reportSection): pull_new 1" in result.output  # the sync itself proceeds


def test_repair_when_both_sides_independently_converge(run_grison, gw_server):
    """Local and remote both changed to the SAME content — not a collision, just a
    stale base to restamp (mirrors the identical "repair" outcome findings/methodology
    reach when both sides drift to an identical value)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.write_text("Converged text.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>Converged text.</p>"

    result = run_grison("sync")

    assert "reports (gw.reportSection): repair 1" in result.output
    assert section.read_text(encoding="utf-8").strip() == "Converged text."
    assert gw_server.operation_log == []  # neither side needed a write, just a restamp


def test_mass_change_guard_withholds_and_announces_it(run_grison, gw_server):
    keys = [f"section_{i}" for i in range(8)]
    _use_fields(gw_server, *keys)
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={k: f"<p>Body {i}.</p>" for i, k in enumerate(keys)},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    for k in keys[:7]:  # 7 of 8 sections edited: > 5 and > 0.2 * 8
        (_rdir("report-a") / "narrative" / f"{k}.md").write_text("Edited.\n", encoding="utf-8")

    result = run_grison("sync")

    assert "MASS-CHANGE GUARD tripped on gw.reportSection — writes withheld." in result.output
    assert gw_server.operation_log == []  # every push was withheld


def test_malformed_local_file_report_isolated_from_other_reports(run_grison, gw_server):
    """A note whose push fails outright (here: an unresolvable operator) does not
    stop a sibling report's own narrative sync — per-record isolation."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.users = []
    gw_server.store.seed_report(
        id=7,
        title="Broken Report",
        extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_report(
        id=8,
        title="Good Report",
        extraFields={"executive_summary": "<p>Fine.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")  # creates both report dirs + narrative
    (_rdir("broken-report") / "notes").mkdir(parents=True, exist_ok=True)
    (_rdir("broken-report") / "notes" / "note.md").write_text("Note.\n", encoding="utf-8")

    result = run_grison("sync")

    assert (_rdir("good-report") / "narrative" / "executive_summary.md").exists()
    assert "could not resolve ghostwriter user id" in result.output.lower()


# --- narrative: sections appearing/disappearing, headings -----------------------


def test_new_section_appears_remotely_on_second_sync(run_grison, gw_server):
    """A brand-new ``extraFieldSpec`` row (an admin adding a field in Ghostwriter)
    appears as a new ``narrative/<field>.md`` on the very next sync — the section
    set is driven by the spec, not by whatever ``extraFields`` happens to contain."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    gw_server.store.seed_report_extra_field_specs(("methodology",))
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["methodology"] = "<p>Newly defined section.</p>"

    result = run_grison("sync")

    assert "reports (gw.reportSection): clean 1, pull_new 1" in result.output
    new_section = _rdir("report-a") / "narrative" / "methodology.md"
    assert new_section.read_text(encoding="utf-8").strip() == "Newly defined section."


def test_extra_fields_value_deleted_still_pulls_as_empty(run_grison, gw_server):
    """Deleting a KEY from ``report.extraFields`` (as opposed to retiring the
    ``extraFieldSpec`` row itself) is no longer "the section vanished" — the spec
    field still exists, so it simply pulls back as empty content."""
    _use_fields(gw_server, "executive_summary", "methodology")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>s</p>", "methodology": "<p>m</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    meth_section = _rdir("report-a") / "narrative" / "methodology.md"
    assert meth_section.read_text(encoding="utf-8").strip() == "m"
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    del report["extraFields"]["methodology"]

    result = run_grison("sync")

    assert "reports (gw.reportSection): clean 1, pull 1" in result.output
    assert meth_section.read_text(encoding="utf-8").strip() == ""


def test_deleting_an_unedited_local_section_clears_it_remotely(run_grison, gw_server):
    """D6: one classification table for every kind — a local file deleted,
    unmodified relative to the last sync, is DELETE_REMOTE, sections included. A
    section has no independent "delete" in Ghostwriter, so this clears the field's
    content back to empty (``NarrativeSectionAdapter.delete``)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>s</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.unlink()

    result = run_grison("sync")

    assert "reports (gw.reportSection): delete_remote 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["extraFields"]["executive_summary"] == ""
    assert not section.exists()  # the local file stays gone — it WAS the delete


def test_heading_levels_round_trip_pull_and_push(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<h2>Overview</h2><h3>Detail</h3><p>Body.</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    pulled = section.read_text(encoding="utf-8")
    assert "## Overview" in pulled
    assert "### Detail" in pulled

    section.write_text("## New Heading\n\nNew body.\n", encoding="utf-8")
    run_grison("sync")

    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "<h2>New Heading</h2>" in report["extraFields"]["executive_summary"]


# --- project.md / .report.yml: regeneration + hand-edit detection ---------------


def test_project_md_regenerates_when_remote_project_data_changes(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"codename": "OP-OLD", "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    ctx = _rdir("report-a") / "project.md"
    assert "OP-OLD" in ctx.read_text(encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["project"]["codename"] = "OP-NEW"

    run_grison("sync")

    assert "OP-NEW" in ctx.read_text(encoding="utf-8")


def test_project_md_hand_edit_is_never_overwritten_and_fails_validation(run_grison, gw_server):
    """Test changed on purpose (was two tests: a "silently overwritten today"
    passing test plus a strict-xfail twin naming the decided fix) — project.md now
    goes through the shared ``write_mirror_guarded`` digest guard (the same one
    ``bs_structure.py`` already used for book/chapter mirrors), so a hand-edit is
    detected and left alone, and ``grison validate`` flags it (WS-009)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"codename": "OP-A", "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    ctx = _rdir("report-a") / "project.md"
    ctx.write_text(ctx.read_text(encoding="utf-8") + "\n\nHand-added note.\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["project"]["codename"] = "OP-NEW"

    run_grison("sync")

    assert "Hand-added note." in ctx.read_text(encoding="utf-8")  # never clobbered
    assert "OP-NEW" not in ctx.read_text(encoding="utf-8")  # remote change did not land either

    validate_result = run_grison("validate")
    assert validate_result.exit_code == 1
    assert "WS-009" in validate_result.output


def test_report_yml_hand_edit_is_never_overwritten_and_fails_validation(run_grison, gw_server):
    """Test changed on purpose — same reasoning as project.md's twin above,
    applied to ``.report.yml``."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    meta = _rdir("report-a") / ".report.yml"
    edited = meta.read_text(encoding="utf-8").replace("title: Report A", "title: HAND-EDITED")
    meta.write_text(edited, encoding="utf-8")

    run_grison("sync")

    assert "HAND-EDITED" in meta.read_text(encoding="utf-8")  # never clobbered

    validate_result = run_grison("validate")
    assert validate_result.exit_code == 1
    assert "WS-009" in validate_result.output


# --- notes: never updated/deleted BY grison, but a remote edit DOES pull ---------


def test_mirrored_note_edited_remotely_is_pulled(run_grison, gw_server):
    """Coordinator correction: ENGINE.md's read-only-kinds rule ("only ever take
    PULL / PULL_NEW / DELETE_LOCAL") applies to append-only's already-indexed half
    too — "never updated ... remotely" (BRIEF task B) means grison itself never
    issues an update/delete mutation, not that a remote edit is ignored. The pull
    lands at the SAME path (D3: stable, index-backed identity — no renaming, no
    second file)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={
            "id": 55,
            "scopes": REPORT_SCOPES,
            "comments": [
                {
                    "id": 10,
                    "note": "<p>Original text</p>",
                    "timestamp": "2026-01-02",
                    "operatorId": 1,
                    "user": {"name": "Lab Admin", "username": "lab"},
                }
            ],
        },
    )
    run_grison("sync")
    ndir = _rdir("report-a") / "notes"
    original_files = sorted(ndir.glob("*.md"))
    assert len(original_files) == 1
    note_path = original_files[0]
    assert "Original text" in note_path.read_text(encoding="utf-8")

    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["project"]["comments"][0]["note"] = "<p>Edited remotely</p>"
    result = run_grison("sync")

    assert "reports (gw.projectNote): pull 1" in result.output
    after_files = sorted(ndir.glob("*.md"))
    assert after_files == original_files  # same path — no new file, nothing renamed
    assert "Edited remotely" in note_path.read_text(encoding="utf-8")
    assert "Original text" not in note_path.read_text(encoding="utf-8")


def test_local_edit_to_a_mirrored_note_is_invalid_never_pushed(run_grison, gw_server):
    """The other half of the append-only fix: a local edit to an ALREADY mirrored
    note can never reach a push — ``grison.engine.classify``'s append-only branch
    clamps it to INVALID (defense in depth; same mechanism read-only mirrors use)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={
            "id": 55,
            "scopes": REPORT_SCOPES,
            "comments": [
                {
                    "id": 10,
                    "note": "<p>Original text</p>",
                    "timestamp": "2026-01-02",
                    "operatorId": 1,
                    "user": {"name": "Lab Admin", "username": "lab"},
                }
            ],
        },
    )
    run_grison("sync")
    note_path = next(iter((_rdir("report-a") / "notes").glob("*.md")))
    note_path.write_text(
        note_path.read_text(encoding="utf-8") + "\nHand-added by mistake.\n",
        encoding="utf-8",
    )

    result = run_grison("sync")

    assert "reports (gw.projectNote): invalid 1" in result.output
    assert "Hand-added by mistake." in note_path.read_text(encoding="utf-8")  # never touched
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["project"]["comments"][0]["note"] == "<p>Original text</p>"  # never pushed


def test_undo_of_a_note_create_deletes_it_and_restores_the_authors_text(run_grison, gw_server):
    """Coordinator correction: undo of a CREATE restores the author's own pre-push
    bytes at the same path (not a bare delete, which would lose their words, and
    not the post-create mirrored form, which was never what they wrote)."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    notes_dir = _rdir("report-a") / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / "team-note.md"
    original_text = "A note for the team.\n"
    note_path.write_text(original_text, encoding="utf-8")
    run_grison("sync")
    assert len(gw_server.store.project_notes) == 1
    assert "author:" in note_path.read_text(encoding="utf-8")  # now the mirrored form

    result = run_grison("undo")

    assert result.exit_code == 0
    assert gw_server.store.project_notes == []
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == original_text  # restored, not deleted

    # and the path is unindexed again — an ordinary re-sync creates it (and only it)
    result2 = run_grison("sync")
    assert "reports (gw.projectNote): create 1" in result2.output
    assert len(gw_server.store.project_notes) == 1


# --- missing-scope lint does not block a sibling report -------------------------


def test_missing_scope_lint_does_not_block_a_sibling_report(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=5,
        title="No Scope",
        extraFields={"executive_summary": "<p>a</p>"},
        project={"codename": "OP-NOSCOPE", "scopes": []},
    )
    gw_server.store.seed_report(
        id=6,
        title="Has Scope",
        extraFields={"executive_summary": "<p>b</p>"},
        project={"scopes": REPORT_SCOPES},
    )

    result = run_grison("sync")

    assert "report 5 (OP-NOSCOPE): project has no scope defined" in result.output
    assert (_rdir("no-scope") / "narrative" / "executive_summary.md").exists()
    assert (_rdir("has-scope") / "narrative" / "executive_summary.md").exists()


# --- pre-write re-fetch guard, via the fake's request hook -----------------------
#
# Test changed on purpose: the old ``_guard_stale_push`` aborted a WHOLE report's
# batched single PUT when an UNTOUCHED section drifted concurrently — a real risk
# under the old design, which merged every pushed section into one ``update_report``
# call. The new per-section design pushes one field at a time, and each push
# independently re-fetches the report's current ``extraFields`` right before
# merging (``NarrativeSectionAdapter._push``) — an untouched field can never be
# clobbered in the first place (see ``test_push_merges_over_a_fresh_fetch_...``
# below), so "abort the whole report" has no equivalent to test. What the ENGINE's
# shared pre-write re-fetch guard (ENGINE.md §3) still catches is a concurrent edit
# to the SAME field being pushed — that's what these two tests now exercise.


def test_stale_push_guard_aborts_push_as_collision_on_concurrent_edit(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>old summary</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir("report-a") / "narrative" / "executive_summary.md"
    es.write_text("new summary\n", encoding="utf-8")

    def concurrent_edit() -> None:
        report = next(r for r in gw_server.store.reports if r["id"] == 7)
        report["extraFields"]["executive_summary"] = "<p>concurrent edit</p>"

    # The pre-write re-fetch guard's report_by_pk call is what must see the
    # concurrent edit — targeted relative to the baseline already spent by the
    # first sync above, rather than a hardcoded absolute count. Classification's
    # own fetch is a bulk `fetch_reports()`, not `report_by_pk` (GWReportContext.
    # refresh) — the guard's refetch is the first `report_by_pk` call this sync
    # makes, before the adapter's own push-time re-fetch-and-merge.
    baseline = gw_server.call_count("report_by_pk")
    gw_server.on_request("report_by_pk", concurrent_edit, call_number=baseline + 1)

    result = run_grison("sync")

    assert "collision" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["extraFields"]["executive_summary"] == "<p>concurrent edit</p>"  # push withheld
    assert es.read_text(encoding="utf-8").strip() == "new summary"  # local edit survives, unsent
    sidecar = es.with_name("executive_summary.remote.md")
    assert sidecar.read_text(encoding="utf-8").strip() == "concurrent edit"


def test_stale_push_guard_withholds_push_when_report_vanishes_mid_run(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>old</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir("report-a") / "narrative" / "executive_summary.md"
    es.write_text("new\n", encoding="utf-8")

    def vanish() -> None:
        gw_server.store.reports[:] = [r for r in gw_server.store.reports if r["id"] != 7]

    baseline = gw_server.call_count("report_by_pk")  # see call-count note above
    gw_server.on_request("report_by_pk", vanish, call_number=baseline + 1)

    result = run_grison("sync")

    # tests changed on purpose: a vanished report is one more shape of "drifted
    # since classification" for the ONE shared pre-write re-fetch guard (ENGINE.md
    # §3) — it becomes a COLLISION like any other drift, not a distinct "gone"
    # outcome/message (the old repmap.py's own bespoke "no longer exists remotely"
    # wording had no engine-wide equivalent to carry forward).
    assert "reports (gw.reportSection): collision 1" in result.output
    assert es.read_text(encoding="utf-8").strip() == "new"  # local edit survives, unsent


# --- closing gaps vs. tests/test_reports.py / test_reports_guards.py -----------


def test_unknown_local_narrative_key_is_invalid_not_pushed(run_grison, gw_server):
    """Test changed on purpose: the old wording ("unknown report field ... skip")
    came from repmap.py's own bespoke narrative-field-name guessing. The new
    validator rule REP-003 makes this an INVALID document (offline, checked
    against ``.report.yml``'s own recorded ``narrative_order`` — no Ghostwriter
    contact needed at validate time) instead of a sync-time skip note."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>s</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    rogue = _rdir("report-a") / "narrative" / "made_up_section.md"
    rogue.write_text("invented\n", encoding="utf-8")

    result = run_grison("sync")

    assert "REP-003" in result.output
    assert "reports (gw.reportSection): clean 1, invalid 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert "made_up_section" not in report["extraFields"]  # never created remotely


def test_pull_surfaces_a_dropped_styling_construct_as_a_loss_event_once(run_grison, gw_server):
    """Restored (coordinator correction) on top of the new engine-wide ``loss``
    event verb: a dropped/canonicalized converter construct surfaces once, on the
    sync that actually writes it to disk, hidden without ``--verbose`` (INFO
    severity) and never repeated on a later, unchanged (CLEAN) sync."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={
            "executive_summary": (
                '<p><span data-color="#ff0000" style="color: #ff0000;">urgent</span></p>'
            ),
        },
        project={"scopes": REPORT_SCOPES},
    )
    r1 = run_grison("sync", "--verbose")
    assert (
        "loss findings/reports/report-a/narrative/executive_summary.md — styling span dropped"
    ) in r1.output

    r2 = run_grison("sync", "--verbose")  # nothing changed — must not re-warn every routine sync
    assert "loss " not in r2.output

    quiet = run_grison("sync")
    assert "loss " not in quiet.output  # INFO severity — hidden without --verbose regardless


def test_project_md_renders_excluded_and_caution_scope_flags(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    scopes = [
        {
            "name": "Internal",
            "scope": "10.0.0.0/8",
            "description": "",
            "disallowed": False,
            "requiresCaution": True,
        },
        {
            "name": "Excluded hosts",
            "scope": "10.9.9.9",
            "description": "",
            "disallowed": True,
            "requiresCaution": False,
        },
    ]
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": scopes},
    )

    run_grison("sync")

    text = (_rdir("report-a") / "project.md").read_text(encoding="utf-8")
    assert "### Internal (CAUTION)" in text
    assert "### Excluded hosts (EXCLUDED)" in text


def test_note_push_dry_run_inserts_nothing_and_leaves_the_file_untouched(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"id": 55, "scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    notes_dir = _rdir("report-a") / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    new_note = notes_dir / "idea.md"
    new_note.write_text("A dry-run idea\n", encoding="utf-8")

    result = run_grison("sync", "--dry-run")

    assert "would create" in result.output
    assert gw_server.store.project_notes == []
    assert new_note.read_text(encoding="utf-8") == "A dry-run idea\n"


def test_bad_narrative_html_is_isolated_other_reports_still_sync(run_grison, gw_server):
    _use_fields(gw_server, "scope_text", "executive_summary")
    gw_server.store.seed_report(
        id=5,
        title="Broken",
        extraFields={"scope_text": "<table><tr><td>x</td></tr></table>"},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_report(
        id=6,
        title="Good",
        extraFields={"executive_summary": "<p>fine</p>"},
        project={"scopes": REPORT_SCOPES},
    )

    run_grison("sync")

    good = _rdir("good") / "narrative" / "executive_summary.md"
    assert good.exists()
    # the broken report's directory + mirrors still materialize (report dirs are a
    # read-only structure, unaffected by one section's own conversion failure), and
    # the OTHER report's fetch/pull is never aborted by it either — a single
    # section's unconvertible remote HTML degrades to a visible (if imperfect) raw
    # rendering instead of raising out of the bulk fetch and taking every report's
    # sync down with it (fetch_remote() itself has no per-record isolation of its
    # own to lean on — see NarrativeSectionAdapter._section_data).
    assert (_rdir("broken") / ".report.yml").exists()
    assert (_rdir("broken") / "narrative" / "scope_text.md").exists()


def test_push_merges_over_a_fresh_fetch_not_the_stale_top_of_run_snapshot(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>old</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir("report-a") / "narrative" / "executive_summary.md"
    es.write_text("new\n", encoding="utf-8")

    def add_out_of_band_field() -> None:
        report = next(r for r in gw_server.store.reports if r["id"] == 7)
        report["extraFields"]["out_of_band"] = "<p>added directly, not through grison</p>"

    baseline = gw_server.call_count("report_by_pk")  # see call-count note above
    gw_server.on_request("report_by_pk", add_out_of_band_field, call_number=baseline + 1)

    result = run_grison("sync")

    # the merge happens silently inside the single-field write — the concurrently
    # added key (not one of this run's spec'd fields) survives, never wiped
    assert "reports (gw.reportSection): push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert report["extraFields"]["executive_summary"] == "<p>new</p>"
    assert report["extraFields"]["out_of_band"] == "<p>added directly, not through grison</p>"


def test_collision_persists_unresolved_across_a_second_sync(run_grison, gw_server):
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>base</p>"},
        project={"scopes": REPORT_SCOPES},
    )
    run_grison("sync")
    es = _rdir("report-a") / "narrative" / "executive_summary.md"
    es.write_text("LOCAL\n", encoding="utf-8")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = "<p>REMOTE</p>"
    run_grison("sync")  # first collision

    result = run_grison("sync")  # still unresolved — must keep surfacing, not silently drop it

    assert "collision" in result.output
    assert es.read_text(encoding="utf-8") == "LOCAL\n"
    assert (
        es.with_name("executive_summary.remote.md").read_text(encoding="utf-8").strip() == "REMOTE"
    )


# --- evidence embeds in narrative sections (D1, shared with gw_findings) --------
#
# Proof for the fix to NarrativeSectionAdapter.canonical_local/canonical_remote and
# IndexRefResolver.to_local (grison.adapters.gw_report / _gw_common): sections now
# go through the SAME grison.engine.filesets.canonical_prose mechanism
# grison.adapters.gw_findings uses — embed ids folded into the hash, captions
# excluded from it, IndexRefResolver.to_local carrying an embed's caption/
# description through from the evidence row instead of dropping them.


def test_captioned_evidence_embed_in_narrative_round_trips_clean(run_grison, gw_server):
    """Before the fix: IndexRefResolver.to_local always returned an empty caption/
    description, so a narrative section's pulled embed permanently disagreed with
    itself on caption text (raw-markdown comparison, no strip) and could never
    settle to CLEAN. Fixed: to_local carries the evidence row's own caption/
    description, and canonical_prose's caption-stripping makes the caption
    irrelevant to the hash either way — a captioned embed is CLEAN immediately
    after the pull that introduced it, and stays CLEAN with the caption intact."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/shot.png",
        friendlyName="shot",
        caption="Login screen",
        description="A close-up of the login form",
    )
    run_grison("sync")  # report dir + empty narrative + evidence/shot.png, all indexed

    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = (
        '<div class="richtext-evidence" data-evidence-id="90"></div>'
    )
    section = _rdir("report-a") / "narrative" / "executive_summary.md"

    pull_result = run_grison("sync")

    assert "reports (gw.reportSection): pull 1" in pull_result.output
    pulled = section.read_text(encoding="utf-8")
    assert '![Login screen](evidence/shot.png "A close-up of the login form")' in pulled

    clean_result = run_grison("sync")  # round trip: nothing changed, must settle CLEAN

    assert "reports (gw.reportSection): clean 1" in clean_result.output
    assert section.read_text(encoding="utf-8") == pulled  # caption/description untouched
    assert gw_server.operation_log == []


def test_narrative_section_cross_reference_and_embed_converge_after_one_push(
    run_grison,
    gw_server,
):
    """Item 1 (engine-findings-lab.md 'Two defects'/#2), narrative sections too:
    the same permanent push loop a reported finding's plain cross-reference
    link (``[caption](evidence/file)``, no ``!``) hit applies identically to a
    narrative section's own ``canonical_prose``/``canonical_remote_prose``
    pairing — fixed by folding both an embed's and a cross-reference's
    identity through the same mechanism, so one push settles it for good."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/login_page.png",
        friendlyName="login_page",
    )
    gw_server.store.seed_evidence(
        id=91,
        reportId=7,
        document="evidence/7/db_dump.png",
        friendlyName="db_dump",
    )
    run_grison("sync")  # downloads + indexes both evidence files

    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.write_text(
        "![Login screenshot](evidence/login_page.png)\n\n"
        "See [DB dump](evidence/db_dump.png) for the resulting database extraction.\n",
        encoding="utf-8",
    )

    pushed = run_grison("sync")
    assert "reports (gw.reportSection): push 1" in pushed.output, pushed.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    body = report["extraFields"]["executive_summary"]
    assert 'data-evidence-id="90"' in body  # the embed
    assert "data-gw-ref-encoded" in body  # the cross-reference

    clean1 = run_grison("sync")
    assert "reports (gw.reportSection): clean 1" in clean1.output, clean1.output
    clean2 = run_grison("sync")
    assert "reports (gw.reportSection): clean 1" in clean2.output, clean2.output


def test_evidence_reupload_pushes_the_narrative_section_with_the_new_id(
    run_grison,
    gw_server,
):
    """D1, verbatim: "replacing an image's bytes must re-push every finding
    referencing it" — automatically, in the SAME run as the reupload, no
    ``--force-local`` and no extra sync.

    Before THIS fix (coordinator correction, phase-reordering task): evidence
    file sets synced in the FINDINGS phase, which runs AFTER the reports phase
    — so a narrative section's own re-push (triggered by its embedded
    evidence's reupload) could only ever be classified on the NEXT sync, one
    run after the reupload actually happened. In between, Ghostwriter's own
    stored narrative text still referenced a DELETED evidence id — a broken
    report export for however long the user waited before syncing again.

    Fixed: :func:`grison.cli._run_reports_phase` now syncs each report's
    ``evidence/`` file set BEFORE its narrative sections/notes — a report's
    ``evidence/`` folder belongs to the report — so by the time
    ``NarrativeSectionAdapter`` classifies, the reupload (if any, this same
    run) has already repointed the index; the classification table's `L !=
    base, R == base` (the LOCAL side's id-fold changed; the REMOTE side's
    ``canonical_remote_prose``-derived literal id didn't, since nothing has
    touched the section's OWN stored HTML yet) reaches PUSH in that SAME
    engine_run call, right after the fileset sync that repointed the index."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/shot.png",
        friendlyName="shot",
        caption="Login screen",
    )
    run_grison("sync")
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    report["extraFields"]["executive_summary"] = (
        '<div class="richtext-evidence" data-evidence-id="90"></div>'
    )
    run_grison("sync")  # pulls the embed; establishes CLEAN state referencing id 90

    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    original_text = section.read_text(encoding="utf-8")
    assert "evidence/shot.png" in original_text

    evidence_file = _rdir("report-a") / "evidence" / "shot.png"
    evidence_file.write_bytes(b"brand new bytes, same filename")
    push_result = run_grison("sync")  # reupload AND the section's own re-push, same run

    assert "reports (gw.evidence[findings/reports/report-a]): move_edit 1" in push_result.output
    assert "reports (gw.reportSection): push 1" in push_result.output
    assert "collision" not in push_result.output
    assert section.read_text(encoding="utf-8") == original_text  # local text never touched
    new_id = next(
        row["id"] for row in gw_server.store.evidence if row["document"].endswith("shot.png")
    )
    assert new_id != 90
    assert f'data-evidence-id="{new_id}"' in report["extraFields"]["executive_summary"]

    before = len(gw_server.operation_log)
    clean_result = run_grison("sync")

    assert "reports (gw.reportSection): clean 1" in clean_result.output
    assert gw_server.operation_log[before:] == []  # nothing left to push — settled


def test_non_ascii_evidence_filename_pushes_from_a_narrative_embed(run_grison, gw_server):
    """grison/markdown/refscan.py item 3: markdown-it-py percent-encodes non-ASCII
    bytes in an image destination — grison.markdown.converter's own markdown-it
    parse hands IndexRefResolver.to_remote a percent-encoded ``src`` on push
    (``evidence/Sonu%C3%A7lar%C4%B1.png``, not ``evidence/Sonuçları.png``), which
    used to fail to resolve against the index (keyed by the real, decoded path)
    and raise ConverterError instead of pushing."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={},
        project={"scopes": REPORT_SCOPES},
    )
    gw_server.store.seed_evidence(
        id=90,
        reportId=7,
        document="evidence/7/Sonuçları.png",
        friendlyName="Sonuçları",
    )
    run_grison("sync")  # downloads + indexes evidence/Sonuçları.png

    section = _rdir("report-a") / "narrative" / "executive_summary.md"
    section.write_text("![Results](evidence/Sonuçları.png)\n", encoding="utf-8")

    result = run_grison("sync")

    assert "reports (gw.reportSection): push 1" in result.output
    report = next(r for r in gw_server.store.reports if r["id"] == 7)
    assert 'data-evidence-id="90"' in report["extraFields"]["executive_summary"]


def test_trailing_empty_blocks_in_sections_and_notes_are_clean_after_a_pull(run_grison, gw_server):
    """Same defect class as the findings parity test: a narrative section or a
    project note whose stored HTML ends in empty blocks/whitespace must not
    classify "edited" right after a clean pull."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Report A",
        extraFields={"executive_summary": "<p>Summary.</p><p></p><h3></h3>\n\n"},
        project={
            "id": 55,
            "scopes": REPORT_SCOPES,
            "comments": [
                {
                    "id": 10,
                    "note": "<p>Note text</p><p></p>\n",
                    "timestamp": "2026-01-02",
                    "operatorId": 1,
                    "user": {"name": "Lab Admin", "username": "lab"},
                }
            ],
        },
    )
    run_grison("sync")

    result = run_grison("sync")

    assert "reports (gw.reportSection): clean 1" in result.output, result.output
    assert "reports (gw.projectNote): clean 1" in result.output, result.output
    assert gw_server.operation_log == []


def test_report_directory_slug_is_transliterated(run_grison, gw_server):
    """`slug(title)` (spec §1.3) transliterates: a Turkish report title gets
    `sizma-testi-raporu`, never the `s-zma-testi-raporu` the ASCII-only rule
    produced on the real workspace."""
    _use_fields(gw_server, "executive_summary")
    gw_server.store.seed_report(
        id=7,
        title="Sızma Testi Raporu",
        extraFields={"executive_summary": "<p>Özet.</p>"},
        project={"scopes": REPORT_SCOPES},
    )

    result = run_grison("sync")

    assert result.exit_code == 0, result.output
    assert (Path.cwd() / "findings" / "reports" / "sizma-testi-raporu" / "project.md").is_file()
