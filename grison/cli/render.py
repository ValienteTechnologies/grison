"""Text/console rendering shared by the CLI's commands — see :mod:`grison.cli`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

import typer

from grison.cli.phases.findings import FindingsPhaseResult
from grison.cli.phases.reports import ReportsPhaseResult
from grison.cli.phases.wiki import WikiPhaseResult
from grison.engine import events as engine_events
from grison.engine.model import Event, KindSummary
from grison.engine.offline_status import OfflineStatus
from grison.sinks import ParseSummary


def _print_version(value: bool) -> None:
    if value:
        from grison import __version__

        typer.echo(__version__)
        raise typer.Exit()


def _format_reasons(reasons: tuple[str, ...]) -> str:
    """``WIKI-010 x38, WIKI-012`` — one entry per distinct rule, with a count when
    it fired more than once, instead of the rule id repeated per failure line."""
    counts: dict[str, int] = {}
    for r in reasons:
        counts[r] = counts.get(r, 0) + 1
    return ", ".join(f"{r} x{n}" if n > 1 else r for r, n in counts.items())


def _print_collision_sidecars(sidecars: list[PurePosixPath], *, area: str) -> None:
    """The one collision-sidecar text block — shared by every area that has one
    (`report`/`methodology` via :func:`_print_offline_non_clean`, and `findings`
    directly, item 12 fix-fin1: a findings-kind collision sidecar used to be
    printed nowhere at all)."""
    if not sidecars:
        return
    typer.echo(f"{area}: {len(sidecars)} collision-sidecar(s) pending")
    for sidecar in sidecars:
        typer.secho(
            f"  ! {sidecar}: unresolved collision — run `grison sync "
            "--force-local`/`--force-remote`",
            fg=typer.colors.RED,
        )


def _print_offline_non_clean(offline: OfflineStatus, *, area: str) -> None:
    _print_collision_sidecars(offline.collision_sidecars, area=area)
    for entry in offline.non_clean:
        color = typer.colors.RED if entry.bucket in ("invalid", "unknown") else None
        detail = ""
        if entry.bucket == "invalid":
            detail = f" ({_format_reasons(entry.reasons)})"
        elif entry.bucket == "moved":
            detail = f" (from {entry.moved_from})"
        elif entry.bucket == "unknown":
            detail = " (no recorded base — never synced through this state store)"
        typer.secho(f"  {entry.bucket:8} {entry.path}{detail}", fg=color, dim=color is None)


def _print_phase_summary(
    label: str,
    events: list[Event],
    summaries: dict[str, KindSummary],
    *,
    verbose: bool = False,
    empty_text: str = "",
    extra: Callable[[], None] | None = None,
) -> None:
    """The event lines + per-kind counts line every phase prints, shared by
    :func:`_print_findings_summary`/``_print_wiki_summary``/``_print_reports_summary``
    below — each supplies its own ``label`` (``"findings"``/``"wiki"``/``"reports"``),
    ``empty_text`` (findings shows ``clean`` for an empty counts line; wiki/reports
    show nothing — an existing difference this keeps, not a new one), and ``extra``
    (the wiki/reports-only structure/dirs lines, printed after the per-kind loop).
    No per-phase "snapshot: ..." line here: one sync run shares ONE snapshot across
    every phase (``grison.cli.commands.sync`` persists it once, after the last phase
    runs, and prints it once itself) — see :mod:`grison.engine.undo`'s module
    docstring."""
    for line in engine_events.render_text_lines(events, verbose=verbose):
        color = None
        if line.startswith(("collision", "invalid", "failed", "withheld")):
            color = typer.colors.RED
        elif line.startswith("skip"):
            color = typer.colors.YELLOW
        typer.secho(line, fg=color, dim=color is None)
    for kind, summary in summaries.items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.counts.items()))
        typer.secho(f"{label} ({kind}): {counts or empty_text}", fg=typer.colors.GREEN)
        if summary.counts.get("withheld"):
            typer.secho(
                f"MASS-CHANGE GUARD tripped on {kind} — writes withheld.", fg=typer.colors.RED
            )
    if extra is not None:
        extra()


def _print_findings_summary(
    findings: FindingsPhaseResult,
    *,
    dry_run: bool,
    verbose: bool = False,
) -> None:
    del dry_run  # no findings-specific tense/count text depends on it
    _print_phase_summary(
        "findings", findings.events, findings.summaries, verbose=verbose, empty_text="clean"
    )


def _print_wiki_summary(wiki: WikiPhaseResult, *, dry_run: bool, verbose: bool = False) -> None:
    del dry_run  # the structure pass's own lines never depend on it (unlike reports')

    def _structure() -> None:
        st = wiki.structure
        if st.created_books or st.created_chapters:
            typer.echo(
                f"structure: create {len(st.created_books)} book(s), "
                f"{len(st.created_chapters)} chapter(s)"
            )
        if st.materialized:
            typer.echo(f"structure: mirror {len(st.materialized)} book/chapter/shelf file(s)")
        for path, reason in st.skipped:
            typer.secho(f"skipped  {path}: {reason}", fg=typer.colors.YELLOW)
        for e in st.errors:
            typer.secho(f"  error: {e}", fg=typer.colors.RED)

    _print_phase_summary("wiki", wiki.events, wiki.summaries, verbose=verbose, extra=_structure)


def _print_reports_summary(
    reports: ReportsPhaseResult,
    *,
    dry_run: bool,
    verbose: bool = False,
) -> None:
    def _dirs() -> None:
        d = reports.dirs
        if d.created:
            tense = "would create" if dry_run else "create"
            typer.echo(f"reports: {tense} {len(d.created)} report dir(s)")
        if d.materialized:
            typer.echo(f"reports: mirror {len(d.materialized)} .report.yml/project.md file(s)")
        for path, reason in d.skipped:
            typer.secho(f"skipped  {path}: {reason}", fg=typer.colors.YELLOW)
        if d.scope_failures:
            typer.secho(f"{len(d.scope_failures)} report(s) missing scope:", fg=typer.colors.RED)
            for msg in d.scope_failures:
                typer.echo(f"  ! {msg}")
        for e in d.errors:
            typer.secho(f"  error: {e}", fg=typer.colors.RED)

    _print_phase_summary("reports", reports.events, reports.summaries, verbose=verbose, extra=_dirs)


def _print_parse_summary(summary: ParseSummary, out_dir: Path, *, dry_run: bool) -> None:
    n_files = sum(summary.files_parsed.values())
    by_scanner = ", ".join(f"{k}: {v}" for k, v in sorted(summary.files_parsed.items()))
    typer.secho(
        f"Parsed {len(summary.findings)} finding(s) from {n_files} file(s)"
        + (f" ({by_scanner})" if by_scanner else ""),
        fg=typer.colors.GREEN,
    )

    sink = summary.sink
    if sink is not None:
        verb = "Would write" if dry_run else "Wrote"
        typer.echo(f"{verb} {len(sink.written)} to {out_dir}  ({len(sink.unchanged)} unchanged)")

    # Refused files (grison.scanners.base.RefusedInput — recognized input a
    # parser deliberately declined, e.g. nmap recon output or pre-v5 sslyze
    # JSON) get their own header and must not also show up under "could not be
    # parsed" below, even though they're also counted in `file_errors` for the
    # exit code (see ParseSummary.refused_files's docstring).
    if summary.refused_files:
        typer.secho(f"{len(summary.refused_files)} file(s) refused:", fg=typer.colors.RED)
        for path, message in summary.refused_files:
            typer.echo(f"  - {path.name}: {message}")

    # Two distinct failure kinds, each printed once, under its own header — a
    # file-level failure (unreadable/unrecognized/bad encoding/refused/parser
    # raised; see ParseSummary.file_errors) is not the same claim as a
    # finding-level one (a finding that DID parse but failed validation or
    # failed to write; see ParseSummary.finding_errors). Mixing them into one
    # "N finding(s) failed validation" block used to mislabel every file-level
    # failure, and printing `skipped_files` AND `errors` both used to print each
    # file-level failure twice.
    refused_strs = {f"{path.name}: {message}" for path, message in summary.refused_files}
    unparsed_errors = [e for e in summary.file_errors if e not in refused_strs]
    if unparsed_errors:
        typer.secho(f"{len(unparsed_errors)} file(s) could not be parsed:", fg=typer.colors.RED)
        for e in unparsed_errors:
            typer.echo(f"  - {e}")

    if summary.warnings:
        typer.secho(f"{len(summary.warnings)} warning(s):", fg=typer.colors.YELLOW)
        for w in summary.warnings:
            typer.echo(f"  - {w}")

    if summary.finding_errors:
        typer.secho(
            f"{len(summary.finding_errors)} finding(s) failed validation:", fg=typer.colors.RED
        )
        for e in summary.finding_errors:
            typer.echo(f"  - {e}")
