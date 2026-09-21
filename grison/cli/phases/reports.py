"""The reports (Ghostwriter report directories/narrative/notes/evidence) sync
phase — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import typer

from grison.adapters import gw_report
from grison.adapters._gw_common import GWContext, GWReportContext
from grison.adapters._gw_common import build_context as build_gw_context
from grison.adapters.gw_evidence import GwEvidenceAdapter
from grison.adapters.gw_notes import ReportNoteAdapter
from grison.adapters.gw_report import NarrativeSectionAdapter
from grison.cli.phases.findings import _apply_caption_rewrites, _report_finding_bodies
from grison.engine.apply import RunOptions
from grison.engine.apply import run as engine_run
from grison.engine.filesets import RunOptions as FilesetRunOptions
from grison.engine.filesets import sync_fileset as engine_sync_fileset
from grison.engine.model import Event, KindSummary, Outcome, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index, IndexKind
from grison.remote.ghostwriter import GhostwriterClient
from grison.validator import validate_workspace


@dataclass
class ReportsPhaseResult:
    """The reports phase's result: report-directory structure (create + mirrors +
    missing-scope trip-wire), plus every :class:`~grison.engine.model.Plan` reached
    across every kind this phase now owns — ``gw.evidence[<report_dir>]`` (one
    file-set summary per report; see :mod:`grison.engine.filesets`),
    ``gw.reportSection``, ``gw.projectNote``.

    D1 ("replacing an image's bytes must re-push every finding referencing it,
    automatically, in the same run"): evidence file sets sync HERE, before
    narrative sections, not in the findings phase — a report's ``evidence/``
    folder belongs to the report, and a narrative section's own re-push (on a
    reupload) must land in the SAME run as the reupload, which only holds if
    the reupload has ALREADY happened by the time ``NarrativeSectionAdapter``
    classifies. The evidence plans/summaries/events counted here (not in
    ``FindingsPhaseResult``) is a deliberate choice, not an accident of where
    the code physically runs — see :func:`_run_reports_phase`'s docstring for
    the exact reasoning and where `grison status`/``--json`` surface it."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    dirs: gw_report.ReportDirResult = field(default_factory=gw_report.ReportDirResult)
    snapshot_dir: Path | None = None

    @property
    def exit_code(self) -> int:
        if any(p.is_problem for p in self.plans) or self.dirs.errors or self.dirs.scope_failures:
            return 1
        return 0


def _reports_relative_force_set(root: Path, paths: set[Path]) -> frozenset[PurePosixPath]:
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts[:2] == ("findings", "reports"):
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


def _run_reports_phase(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    allow_mass_change: bool = False,
    snapshot: Snapshot,
    quiet: bool = False,
) -> tuple[ReportsPhaseResult, dict[int, dict[int, dict[str, Any]]]]:
    """The reports phase: report directories + `.report.yml`/`project.md` mirrors
    (:func:`grison.adapters.gw_report.sync_report_dirs` — structure-style, like the
    wiki's book/chapter pass); THEN, per indexed report, its ``evidence/`` file set
    (:mod:`grison.engine.filesets` — moved here from the findings phase, D1: "a
    reupload must re-push every finding referencing it, automatically, in the same
    run" — this only holds for a NARRATIVE section's own reupload-triggered re-push
    if the reupload has already happened by the time ``NarrativeSectionAdapter``
    classifies, and the reports phase runs before the findings phase, so evidence
    has to sync here to land in the SAME run at all); THEN narrative sections and
    project notes through the engine, using the evidence rows the fileset sync just
    established (``evidence_by_report``, returned alongside the result — the
    findings phase's ``GwReportedFindingAdapter`` needs the exact same rows, built
    only ONCE here and shared rather than re-fetched, per the coordinator's
    instruction). The validation gate is scoped to ``findings/reports`` — this
    phase only pushes/pulls report-owned records (narrative sections, notes);
    a reported/library finding document's own gate is the findings phase's
    ``findings/``-scoped one instead (:func:`_run_findings_phase`).

    Evidence's own plans/summaries/events land in THIS phase's result (a deliberate
    choice — the alternative, folding them into ``FindingsPhaseResult`` instead, was
    considered and rejected: evidence now sync BEFORE, not alongside, the findings
    engine runs, so counting them there would misdescribe when/where the work
    happened; ``grison status``/``--json`` read ``ReportsPhaseResult.summaries``'s
    ``gw.evidence[<report_dir>]`` keys exactly as before, just attributed to the
    "report" phase instead of "findings" in the per-phase last-sync bookkeeping).

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot` — the
    findings and (if it runs) wiki phases append to the SAME object; this phase
    (the first one to run) never persists it and never creates its own (one undo
    snapshot per sync run, not one per phase — see :mod:`grison.engine.undo`'s
    module docstring).

    ``quiet`` (item 8, fix-fin1): see :func:`_run_wiki_phase`'s own docstring —
    same reason (suppress the dir-mirror pass's raw ``typer.secho`` progress
    lines under ``--json``, never valid JSON on their own)."""
    index = Index.load(root)
    state = StateStore(root)
    ctx = build_gw_context(client, index)

    dirs = gw_report.sync_report_dirs(
        root,
        ctx,
        index,
        state,
        snapshot,
        dry_run=dry_run,
        on_event=None if quiet else lambda msg: typer.secho(msg, dim=True),
    )
    _refresh_report_dirs(ctx, index)

    all_failures = validate_workspace(root, paths=[root / "findings" / "reports"])
    report_failures = [f for f in all_failures if f.path.startswith("findings/reports")]

    fl = _reports_relative_force_set(root, force_local)
    fr = _reports_relative_force_set(root, force_remote)
    options = RunOptions(
        dry_run=dry_run, force_local=fl, force_remote=fr, allow_mass_change=allow_mass_change
    )
    fs_options = FilesetRunOptions(
        dry_run=dry_run, force_local=fl, force_remote=fr, allow_mass_change=allow_mass_change
    )

    plans: list[Plan] = []
    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}

    # --- evidence file sets, one per indexed report (D1 — see docstring above) ---
    evidence_ctx = GWContext.build(client, index)
    for report_dir in sorted(evidence_ctx.report_dirs):
        report_id = evidence_ctx.report_dirs[report_dir]
        evidence_adapter = GwEvidenceAdapter(report_id=report_id)
        doc_bodies = _report_finding_bodies(root, report_dir)
        evidence_dir = report_dir / "evidence"
        try:
            fs_result = engine_sync_fileset(
                root,
                evidence_ctx,
                evidence_adapter,
                evidence_dir,
                index=index,
                state=state,
                snapshot=snapshot,
                doc_bodies=doc_bodies,
                options=fs_options,
                failures=report_failures,
            )
        except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5):
            # one report's evidence file set blowing up must not abort every other
            # report/section in this phase, any more than one record's own apply
            # step does inside grison.engine.filesets/apply themselves.
            reason = f"{type(e).__name__}: {e}"
            events.append(Event(verb="failed", path=str(evidence_dir), detail=reason))
            plans.append(
                Plan(
                    kind=evidence_adapter.kind,
                    outcome=Outcome.FAILED,
                    path=evidence_dir,
                    reason=reason,
                )
            )
            summaries[f"gw.evidence[{report_dir}]"] = KindSummary(
                kind=evidence_adapter.kind,
                counts={"failed": 1},
                problem_paths=[str(evidence_dir)],
            )
            continue
        # a fileset's own problems (collision/failed/withheld evidence) must count
        # toward THIS phase's exit code exactly like a section's would — folded
        # into the SAME plans list ReportsPhaseResult.exit_code reads, not just
        # reported as text/JSON events that a bad run's exit code would then miss.
        plans.extend(fs_result.plans)
        events.extend(fs_result.events)
        summaries[f"gw.evidence[{report_dir}]"] = fs_result.summary
        if not dry_run and fs_result.resolved_captions:
            _apply_caption_rewrites(
                root, doc_bodies, fs_result.resolved_captions, folder_name="evidence"
            )

    # Evidence rows, re-fetched fresh (post-write) for every report, so the
    # narrative/findings adapters' RefResolvers see ids/captions/friendly-names as
    # they actually are right now, not as of the start of this sync — built ONCE
    # here and returned for the findings phase to reuse (never a second org-wide
    # fetch for the same data).
    evidence_by_report: dict[int, dict[int, dict[str, Any]]] = {}
    for report_id in evidence_ctx.report_dirs.values():
        rows = GwEvidenceAdapter(report_id=report_id).list_remote(evidence_ctx)
        evidence_by_report[report_id] = {i: r.data for i, r in rows.items()}

    # --- narrative sections + project notes ---------------------------------
    for adapter in (
        NarrativeSectionAdapter(evidence_by_report=evidence_by_report),
        ReportNoteAdapter(),
    ):
        p, adapter_events, s = engine_run(
            root,
            ctx,
            adapter,
            index=index,
            state=state,
            snapshot=snapshot,
            failures=report_failures,
            options=options,
        )
        plans.extend(p)
        events.extend(adapter_events)
        summaries[adapter.kind] = s

    if not dry_run:
        index.save()

    result = ReportsPhaseResult(
        plans=plans,
        events=events,
        summaries=summaries,
        dirs=dirs,
    )
    return result, evidence_by_report


def _refresh_report_dirs(ctx: GWReportContext, index: Index) -> None:
    """Repopulate ``ctx.dir_by_report_id``/``report_id_by_dir`` from the index after
    :func:`grison.adapters.gw_report.sync_report_dirs` has (possibly) created a new
    report directory — the narrative/notes adapters resolve a report's directory
    purely through these maps, never the index directly."""
    ctx.dir_by_report_id = {}
    ctx.report_id_by_dir = {}
    for path, rec in index.records.items():
        if rec.kind is IndexKind.GW_REPORT:
            name = PurePosixPath(path).name
            ctx.dir_by_report_id[rec.id] = name
            ctx.report_id_by_dir[name] = rec.id


def _report_dirs_for_status(index: Index) -> dict[PurePosixPath, int]:
    return {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind is IndexKind.GW_REPORT
    }
