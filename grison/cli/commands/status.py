"""``grison status`` — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from grison.adapters.bs_pages import BsPageAdapter
from grison.adapters.gw_findings import GwLibraryFindingAdapter, GwReportedFindingAdapter
from grison.adapters.gw_notes import ReportNoteAdapter
from grison.adapters.gw_report import NarrativeSectionAdapter
from grison.cli import app, clients
from grison.cli.guards import _guarded, _refuse_if_format_mismatch
from grison.cli.payloads import _entry_json, _fileset_json, _fileset_status_line, _remote_phase_json
from grison.cli.phases import findings as findings_phase
from grison.cli.phases import reports as reports_phase
from grison.cli.phases import wiki as wiki_phase
from grison.cli.render import _print_collision_sidecars, _print_offline_non_clean
from grison.engine.model import KindSummary
from grison.engine.offline_status import OfflineStatus, compute_offline_status
from grison.engine.sidecar import is_sidecar_name
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.errors import GrisonError
from grison.index import Index
from grison.remote.creds import load as load_creds
from grison.validator import find_workspace_root, validate_workspace
from grison.workspace import bootstrap_tree


@app.command(help="Show what changed in the workspace since the last sync.")
@_guarded
def status(
    remote: Annotated[
        bool,
        typer.Option(
            "--remote",
            help="Also ask the servers what the next sync would do (writes nothing).",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
) -> None:
    """Dev notes (user-facing behavior is in ``--help``/README's ``## Commands``):

    ``--remote`` reuses the SAME dry-run phase functions ``grison sync`` itself
    calls (report, findings, then wiki) — never a second classification
    implementation — so it can never drift from what a real sync would do.

    Exit code: 0 clean, 1 something needs attention — an invalid record, an
    unknown one (no recorded base to compare against), or a live collision
    sidecar; an ordinary pending edit/new/deleted/moved record is not itself a
    problem (matches ``grison validate``'s own policy — ENGINE.md §10) — 2 could
    not run (no workspace).
    """
    try:
        root = find_workspace_root(Path.cwd())
    except GrisonError as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    _refuse_if_format_mismatch(root)  # D13 (item 10) — before any work

    failures = validate_workspace(root)
    state = StateStore(root)
    last_sync = state.load_last_sync()
    phases = last_sync.get("phases", {}) if last_sync else {}

    index = Index.load(root)
    findings_failures = [f for f in failures if f.path.startswith("findings")]
    wiki_failures = [f for f in failures if f.path.startswith("methodology")]
    offline = compute_offline_status(root, BsPageAdapter(), index, state, wiki_failures)
    lib_offline = compute_offline_status(
        root,
        GwLibraryFindingAdapter(),
        index,
        state,
        findings_failures,
    )
    rf_offline = compute_offline_status(
        root,
        GwReportedFindingAdapter(index=index, evidence_by_report={}),
        index,
        state,
        findings_failures,
    )
    evidence_counts = {
        d: _fileset_status_counts(root / d / "evidence")
        for d in reports_phase._report_dirs_for_status(index)
        if (root / d / "evidence").is_dir()
    }
    image_counts = {
        d: _fileset_status_counts(root / d / "images")
        for d in wiki_phase._book_dirs(index)
        if (root / d / "images").is_dir()
    }

    reports_failures = [f for f in failures if f.path.startswith("findings/reports")]
    reports_offline = _merge_offline(
        [
            compute_offline_status(root, NarrativeSectionAdapter(), index, state, reports_failures),
            compute_offline_status(root, ReportNoteAdapter(), index, state, reports_failures),
        ]
    )

    # `--remote`: reuse the SAME phase functions `grison sync` calls, all forced to
    # `dry_run=True` — never a second classification implementation (each phase
    # function already loads its own scratch `Index` copy and never calls
    # `index.save()`/persists a snapshot/writes state under dry_run, exactly what
    # `grison sync --dry-run` itself relies on and is tested for). Ghostwriter
    # (report -> findings) and BookStack (wiki) are independent legs: either can be
    # unconfigured/fail without blocking the other.
    remote_summaries: dict[str, dict[str, KindSummary]] = {}
    remote_gw_error: str | None = None
    remote_bs_error: str | None = None
    if remote:
        # The phase functions below scope `validate_workspace` to `findings/reports`/
        # `methodology` (an explicit path that doesn't exist raises, not "nothing to
        # validate") — `grison sync` never hits this because `bootstrap_workspace`
        # always runs first; `status` never otherwise writes anything, so it can't
        # call that (CLAUDE.md/.claude/settings.json/etc). `bootstrap_tree` is the
        # narrow part of it: the five plain, empty workspace directories only — no
        # file is created, so this never shows up in a before/after byte comparison.
        bootstrap_tree(root)
        creds = load_creds(root)
        if creds.gw_url and creds.gw_token:
            try:
                with clients._make_gw_client(creds) as gw_client:
                    reports_result, evidence_by_report = reports_phase._run_reports_phase(
                        root,
                        gw_client,
                        dry_run=True,
                        force_local=set(),
                        force_remote=set(),
                        snapshot=Snapshot(),
                        quiet=json_output,
                    )
                    remote_summaries["report"] = reports_result.summaries
                    findings_result = findings_phase._run_findings_phase(
                        root,
                        gw_client,
                        dry_run=True,
                        force_local=set(),
                        force_remote=set(),
                        evidence_by_report=evidence_by_report,
                        snapshot=Snapshot(),
                    )
                    remote_summaries["findings"] = findings_result.summaries
            except GrisonError as e:
                remote_gw_error = str(e)
        else:
            remote_gw_error = "Ghostwriter credentials not configured"

        if creds.bs_url and creds.bs_token_id and creds.bs_token_secret:
            try:
                with clients._make_bs_client(creds) as bs_client:
                    wiki_result = wiki_phase._run_wiki_phase(
                        root,
                        bs_client,
                        dry_run=True,
                        force_local=set(),
                        force_remote=set(),
                        snapshot=Snapshot(),
                        quiet=json_output,
                    )
                    remote_summaries["wiki"] = wiki_result.summaries
            except GrisonError as e:
                remote_bs_error = str(e)
        else:
            remote_bs_error = "BookStack credentials not configured"

    # item 12, fix-fin1: a findings-kind (library or reported) collision sidecar
    # used to be invisible from `grison status` entirely — neither counted toward
    # the exit code nor surfaced in `--json`/text output, unlike `report`'s and
    # `methodology`'s own top-level `collision_sidecars`. One merged view (same
    # helper `reports_offline` already uses to combine ITS two engine-managed
    # kinds) makes `findings.collision_sidecars` a real, first-class field.
    findings_offline = _merge_offline([lib_offline, rf_offline])
    offline_problems = bool(
        offline.counts["invalid"]
        or offline.counts["unknown"]
        or offline.collision_sidecars
        or lib_offline.counts["invalid"]
        or lib_offline.counts["unknown"]
        or rf_offline.counts["invalid"]
        or rf_offline.counts["unknown"]
        or findings_offline.collision_sidecars
    )
    reports_problems = bool(
        reports_offline.counts["invalid"]
        or reports_offline.counts["unknown"]
        or reports_offline.collision_sidecars
    )
    problems = offline_problems or reports_problems or _remote_has_problems(remote_summaries)

    if json_output:
        payload = {
            "findings": {
                "managed": True,
                "collision_sidecars": [str(p) for p in findings_offline.collision_sidecars],
                "library": {
                    "counts": lib_offline.counts,
                    "non_clean": [_entry_json(e) for e in lib_offline.non_clean],
                },
                "reports": {
                    "counts": rf_offline.counts,
                    "non_clean": [_entry_json(e) for e in rf_offline.non_clean],
                },
            },
            "report": {
                "managed": True,
                "counts": reports_offline.counts,
                "non_clean": [_entry_json(e) for e in reports_offline.non_clean],
                "collision_sidecars": [str(p) for p in reports_offline.collision_sidecars],
                "evidence": {str(d): _fileset_json(c) for d, c in evidence_counts.items()},
            },
            "methodology": {
                "managed": True,
                "counts": offline.counts,
                "non_clean": [_entry_json(e) for e in offline.non_clean],
                "collision_sidecars": [str(p) for p in offline.collision_sidecars],
                "images": {str(d): _fileset_json(c) for d, c in image_counts.items()},
            },
            "remote": (
                {
                    "report": _remote_phase_json(remote_summaries, "report", remote_gw_error),
                    "findings": _remote_phase_json(remote_summaries, "findings", remote_gw_error),
                    "wiki": _remote_phase_json(remote_summaries, "wiki", remote_bs_error),
                }
                if remote
                else None
            ),
            "last_sync": {
                "findings": phases.get("findings"),
                "report": phases.get("report"),
                "wiki": phases.get("wiki"),
            },
        }
        typer.echo(json.dumps(payload, indent=2))
        if problems:
            raise typer.Exit(code=1)
        return

    lib_line = ", ".join(f"{b} {n}" for b, n in lib_offline.counts.items() if n)
    rf_line = ", ".join(f"{b} {n}" for b, n in rf_offline.counts.items() if n)
    typer.echo(f"findings (library): {lib_line or 'clean'}")
    typer.echo(f"findings (reports): {rf_line or 'clean'}")
    _print_collision_sidecars(findings_offline.collision_sidecars, area="findings")
    reports_counts_line = ", ".join(f"{b} {n}" for b, n in reports_offline.counts.items() if n)
    typer.echo(f"report: {reports_counts_line or 'clean'}")
    if evidence_counts:
        typer.echo(f"evidence: {_fileset_status_line(evidence_counts)}")
    _print_offline_non_clean(reports_offline, area="report")
    counts_line = ", ".join(f"{b} {n}" for b, n in offline.counts.items() if n)
    typer.echo(f"methodology: {counts_line or 'clean'}")
    if image_counts:
        typer.echo(f"images: {_fileset_status_line(image_counts)}")
    _print_offline_non_clean(offline, area="methodology")
    if remote:
        any_kind_line = False
        for phase in ("report", "findings", "wiki"):
            for kind, summary in sorted(remote_summaries.get(phase, {}).items()):
                counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.counts.items()) if v)
                if not counts:
                    continue
                any_kind_line = True
                typer.echo(f"remote {phase} ({kind}): {counts}")
                for problem_path in summary.problem_paths:
                    typer.secho(f"  ! {problem_path}", fg=typer.colors.RED)
        if remote_gw_error is not None:
            typer.secho(f"remote (ghostwriter): {remote_gw_error}", fg=typer.colors.YELLOW)
        if remote_bs_error is not None:
            typer.secho(f"remote (bookstack): {remote_bs_error}", fg=typer.colors.YELLOW)
        if not any_kind_line and remote_gw_error is None and remote_bs_error is None:
            typer.echo("remote (--remote, dry-run): clean")
    for phase in ("findings", "report", "wiki"):
        info = phases.get(phase)
        if info is None:
            typer.echo(f"last {phase} sync: never")
        else:
            tag = "ok" if info.get("ok") else f"FAILED — {info.get('error', 'see prior output')}"
            typer.echo(f"last {phase} sync: {info.get('at', '?')} ({tag})")

    if problems:
        raise typer.Exit(code=1)


def _remote_has_problems(remote_summaries: dict[str, dict[str, KindSummary]]) -> bool:
    """``--remote``'s exit-code contribution: any kind, in any phase, that reached a
    problem outcome (matches ``grison sync``'s own result/exit-code policy,
    ENGINE.md §10) — a credentials-not-configured/transport error is reported but
    never itself flips the exit code (same as the offline-only ``remote_error``
    behaviour this replaces)."""
    return any(
        k in summary.counts
        for phase_summaries in remote_summaries.values()
        for summary in phase_summaries.values()
        for k in ("collision", "invalid", "failed", "withheld")
    )


@dataclass(frozen=True)
class _FilesetCounts:
    """One file-set folder's offline-visible shape (evidence/images, item 5): a
    live collision sidecar (``grison.engine.sidecar.is_sidecar_name``) is listed
    as a collision, never counted as a file — mirrors the same sidecar-aware
    treatment ``grison sync``/``last-sync.json`` already give it, so ``grison
    status`` never shows an extra "file" a real sync wouldn't otherwise write."""

    files: int
    collisions: int


def _fileset_status_counts(dir_path: Path) -> _FilesetCounts:
    files = collisions = 0
    for p in sorted(dir_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if is_sidecar_name(p.name):
            collisions += 1
        else:
            files += 1
    return _FilesetCounts(files=files, collisions=collisions)


def _merge_offline(statuses: list[OfflineStatus]) -> OfflineStatus:
    """Combine per-kind offline statuses (``findings/reports`` has two engine-managed
    kinds, ``gw.reportSection`` and ``gw.projectNote``) into one view for `grison
    status` — every rule already files its own failure/bucket per path, so a plain
    concatenation is exact, never a re-derived summary."""
    entries = sorted((e for s in statuses for e in s.entries), key=lambda e: str(e.path))
    sidecars = sorted({p for s in statuses for p in s.collision_sidecars})
    return OfflineStatus(entries=entries, collision_sidecars=sidecars)
