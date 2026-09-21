"""The findings sync phase — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWContext
from grison.adapters.gw_findings import GwLibraryFindingAdapter, GwReportedFindingAdapter
from grison.engine.apply import RunOptions
from grison.engine.apply import run as engine_run
from grison.engine.filesets import rewrite_captions
from grison.engine.model import Event, KindSummary, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.fsio import atomic_write_text
from grison.index import Index
from grison.remote.ghostwriter import GhostwriterClient
from grison.validator import validate_workspace


def _findings_relative_force_set(root: Path, paths: set[Path]) -> frozenset[PurePosixPath]:
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "findings":
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


@dataclass
class FindingsPhaseResult:
    """The findings phase's result: library findings + reported findings, both
    through :mod:`grison.engine.apply`. Evidence file sets sync in the REPORTS
    phase now, before this one — see :func:`grison.cli._run_reports_phase`'s
    docstring (D1: a reupload's re-push must land in the same run as the
    reupload, which requires evidence to sync before whatever references it) —
    so ``ReportsPhaseResult``, not this one, carries evidence's own plans/
    summaries/events."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    snapshot_dir: Path | None = None

    @property
    def exit_code(self) -> int:
        return 1 if any(p.is_problem for p in self.plans) else 0


def _report_finding_bodies(root: Path, report_dir: PurePosixPath) -> dict[PurePosixPath, str]:
    """Every reported-finding document currently on disk in ``report_dir`` (top
    level only — narrative/notes/evidence are different record types), for
    :func:`grison.engine.filesets.collect_captions`'s caption-agreement scan."""
    out: dict[PurePosixPath, str] = {}
    d = root / report_dir
    if not d.is_dir():
        return out
    for md in d.glob("*.md"):
        if md.name.endswith(".remote.md"):
            continue
        out[PurePosixPath(md.relative_to(root).as_posix())] = md.read_text(encoding="utf-8")
    return out


def _apply_caption_rewrites(
    root: Path,
    doc_bodies: dict[PurePosixPath, str],
    resolved: dict[str, tuple[str, str]],
    *,
    folder_name: str,
) -> None:
    """The PULL-side half of D1's caption rule (module docstring of
    :mod:`grison.engine.filesets`): rewrite every embed's alt/title in every
    referencing document to match the evidence set's resolved caption. Never
    touches state/index — a referencing document's canonical payload excludes
    captions by construction, so this can never make it look edited."""
    for path, body in doc_bodies.items():
        rewritten = rewrite_captions(body, resolved, folder_name=folder_name)
        if rewritten != body:
            atomic_write_text(root / path, rewritten)


def _run_findings_phase(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    allow_mass_change: bool = False,
    evidence_by_report: dict[int, dict[int, dict[str, Any]]],
    snapshot: Snapshot,
) -> FindingsPhaseResult:
    """The findings phase (BRIEF engine step 3): library findings, then reported
    findings across every report (one adapter/kind — see
    :mod:`grison.adapters.gw_findings`'s module docstring on cross-report moves).
    Evidence file sets no longer sync here — see :func:`_run_reports_phase`'s
    docstring (D1) — so ``evidence_by_report`` (that phase's own result, built
    ONCE right after its evidence-file-set sync) is a required parameter, not
    something this phase re-fetches; the fresh-index dependency that made "report
    phase, then findings phase" work at all (a reupload's new id, or a brand-new
    report directory, must already be on disk before this phase's own
    ``Index.load`` below) is unchanged — it was always ``grison.cli.sync``'s
    sequential phase calls (each phase persists its index before the next one
    loads), never anything specific to where evidence used to sync. Validation is
    scoped to ``findings/`` here, same pattern as the wiki phase's own
    ``methodology/`` scoping.

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot` — the same
    object the report and (if it runs) wiki phases also append to and that
    ``sync`` alone persists, once, after every phase has run (one undo snapshot
    per sync run, not one per phase — see :mod:`grison.engine.undo`'s module
    docstring). This phase never persists it and never creates its own."""
    index = Index.load(root)
    state = StateStore(root)
    ctx = GWContext.build(client, index)

    all_failures = validate_workspace(root, paths=[root / "findings"])
    findings_failures = [f for f in all_failures if f.path.startswith("findings")]

    fl = _findings_relative_force_set(root, force_local)
    fr = _findings_relative_force_set(root, force_remote)
    options = RunOptions(
        dry_run=dry_run, force_local=fl, force_remote=fr, allow_mass_change=allow_mass_change
    )

    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}

    lib_plans, lib_events, lib_summary = engine_run(
        root,
        client,
        GwLibraryFindingAdapter(),
        index=index,
        state=state,
        snapshot=snapshot,
        failures=findings_failures,
        options=options,
    )
    events.extend(lib_events)
    summaries[GwLibraryFindingAdapter.kind] = lib_summary

    rf_adapter = GwReportedFindingAdapter(index=index, evidence_by_report=evidence_by_report)
    rf_plans, rf_events, rf_summary = engine_run(
        root,
        ctx,
        rf_adapter,
        index=index,
        state=state,
        snapshot=snapshot,
        failures=findings_failures,
        options=options,
    )
    events.extend(rf_events)
    summaries[GwReportedFindingAdapter.kind] = rf_summary

    if not dry_run:
        index.save()

    return FindingsPhaseResult(
        plans=[*lib_plans, *rf_plans],
        events=events,
        summaries=summaries,
    )
