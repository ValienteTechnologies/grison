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
from grison.cli.phases.common import FilesetItem, PhaseCtx, PhaseSpec, run_phase
from grison.cli.phases.findings import _apply_caption_rewrites, _report_finding_bodies
from grison.engine.model import Event, KindSummary, Plan
from grison.engine.undo import Snapshot
from grison.index import Index, IndexKind
from grison.remote.ghostwriter import GhostwriterClient


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
    instruction).

    Unlike the wiki phase, ``methodology/`` validation runs BEFORE its structure
    pass — this phase's own ``structure`` hook (below) re-validates ``findings/
    reports`` AFTER ``sync_report_dirs``/``_refresh_report_dirs`` run, so a report
    directory or mirror this very run just created/materialized is already
    reflected in the failures that gate its own evidence/narrative/notes sync,
    instead of only being checked on the NEXT sync. The validation gate is scoped
    to ``findings/reports`` — this phase only pushes/pulls report-owned records
    (narrative sections, notes); a reported/library finding document's own gate is
    the findings phase's ``findings/``-scoped one instead.

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

    ``quiet`` (item 8, fix-fin1): see :func:`grison.cli.phases.wiki._run_wiki_phase`'s
    own docstring — same reason (suppress the dir-mirror pass's raw ``typer.secho``
    progress lines under ``--json``, never valid JSON on their own)."""

    def build_ctx(pctx: PhaseCtx) -> None:
        pctx.extra["ctx"] = build_gw_context(client, pctx.index)

    def structure(pctx: PhaseCtx) -> None:
        pctx.extra["dirs"] = gw_report.sync_report_dirs(
            pctx.root,
            pctx.extra["ctx"],
            pctx.index,
            pctx.state,
            pctx.snapshot,
            dry_run=pctx.dry_run,
            on_event=None if pctx.quiet else lambda msg: typer.secho(msg, dim=True),
        )
        _refresh_report_dirs(pctx.extra["ctx"], pctx.index)

    def fileset_items(pctx: PhaseCtx) -> list[FilesetItem]:
        evidence_ctx = GWContext.build(client, pctx.index)
        pctx.extra["evidence_ctx"] = evidence_ctx
        items: list[FilesetItem] = []
        for report_dir in sorted(evidence_ctx.report_dirs):
            report_id = evidence_ctx.report_dirs[report_dir]
            adapter = GwEvidenceAdapter(report_id=report_id)
            doc_bodies = _report_finding_bodies(pctx.root, report_dir)
            items.append((report_dir, report_dir / "evidence", evidence_ctx, adapter, doc_bodies))
        return items

    def fileset_on_success(
        pctx: PhaseCtx,
        folder: PurePosixPath,
        doc_bodies: dict[PurePosixPath, str] | None,
        fs_result: Any,
    ) -> None:
        del folder
        if not pctx.dry_run and fs_result.resolved_captions:
            _apply_caption_rewrites(
                pctx.root, doc_bodies or {}, fs_result.resolved_captions, folder_name="evidence"
            )

    def post_fileset(pctx: PhaseCtx) -> None:
        # Evidence rows, re-fetched fresh (post-write) for every report, so the
        # narrative/findings adapters' RefResolvers see ids/captions/friendly-names
        # as they actually are right now, not as of the start of this sync — built
        # ONCE here and returned for the findings phase to reuse (never a second
        # org-wide fetch for the same data).
        evidence_ctx = pctx.extra["evidence_ctx"]
        evidence_by_report: dict[int, dict[int, dict[str, Any]]] = {}
        for report_id in evidence_ctx.report_dirs.values():
            rows = GwEvidenceAdapter(report_id=report_id).list_remote(evidence_ctx)
            evidence_by_report[report_id] = {i: r.data for i, r in rows.items()}
        pctx.extra["evidence_by_report"] = evidence_by_report

    def engine_steps(pctx: PhaseCtx) -> list[tuple[Any, Any]]:
        ctx = pctx.extra["ctx"]
        evidence_by_report = pctx.extra["evidence_by_report"]
        return [
            (ctx, NarrativeSectionAdapter(evidence_by_report=evidence_by_report)),
            (ctx, ReportNoteAdapter()),
        ]

    spec = PhaseSpec(
        name="report",
        validate_subtree="findings/reports",
        force_prefix=("findings", "reports"),
        build_ctx=build_ctx,
        structure=structure,
        fileset_items=fileset_items,
        fileset_on_success=fileset_on_success,
        post_fileset=post_fileset,
        engine_steps=engine_steps,
    )
    result = run_phase(
        root,
        client,
        spec,
        dry_run=dry_run,
        force_local=force_local,
        force_remote=force_remote,
        allow_mass_change=allow_mass_change,
        snapshot=snapshot,
        quiet=quiet,
    )
    phase_result = ReportsPhaseResult(
        plans=result.plans,
        events=result.events,
        summaries=result.summaries,
        dirs=result.extra["dirs"],
    )
    return phase_result, result.extra["evidence_by_report"]


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
