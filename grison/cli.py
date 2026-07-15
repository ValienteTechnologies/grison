"""grison CLI — three verbs: ``parse``, ``status``, ``sync``.

The path names the backend (``findings/`` ⇄ Ghostwriter, ``methodology/`` ⇄
BookStack); location decides identity; the first ``sync`` bootstraps the workspace.
``parse`` and ``status`` are offline; ``sync`` reconciles findings with Ghostwriter
and methodology with BookStack (push/pull/collision derived per record).
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from grison.model import FindingType
from grison.remote.bookstack import BookStackClient
from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.creds import MissingCreds
from grison.remote.creds import load as load_creds
from grison.remote.ghostwriter import GhostwriterClient
from grison.remote.methodology import MethResult, sync_methodology
from grison.remote.sync import SyncResult
from grison.remote.sync import sync as run_sync
from grison.sinks import ParseSummary, run_parse
from grison.validate import validate_file
from grison.workspace import bootstrap_tree, inbox_dir

app = typer.Typer(
    name="grison",
    help="A markdown hub between security scanners and Valiente's Ghostwriter + BookStack.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """grison — parse scanner artifacts to markdown, then status/sync with the remotes."""


@app.command()
def parse(
    paths: Annotated[list[Path], typer.Argument(help="Scanner export file(s) or dir(s).")],
    scanner: Annotated[
        str | None,
        typer.Option("--scanner", help="Force a scanner type instead of auto-detecting."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("-o", "--out", help="Output dir (default: findings/inbox/)."),
    ] = None,
    finding_type: Annotated[
        FindingType | None,
        typer.Option("--finding-type", help="Override the per-scanner finding-type default."),
    ] = None,
    min_severity: Annotated[
        str | None,
        typer.Option("--min-severity", help="Keep only e.g. 'high,critical' or 'medium-critical'."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing.")] = False,
) -> None:
    """Turn scanner export(s) into markdown findings in findings/inbox/ (offline)."""
    if out is None:
        bootstrap_tree(Path.cwd())  # the binary scaffolds; no init
        out_dir = inbox_dir(Path.cwd())
    else:
        out_dir = out
    summary = run_parse(
        paths,
        out_dir,
        scanner=scanner,
        finding_type=finding_type,
        min_severity=min_severity,
        dry_run=dry_run,
    )
    _print_parse_summary(summary, out_dir, dry_run=dry_run)
    if summary.errors:
        raise typer.Exit(code=1)


@app.command()
def status(
    paths: Annotated[list[Path], typer.Argument(help="Finding markdown file(s) or dir(s).")],
) -> None:
    """Report per-record validity (schema / enum / CVSS / CWE / GW whitelist)."""
    files = _resolve_md(paths)
    if not files:
        typer.secho("no markdown files found", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    invalid = 0
    for f in files:
        errors = validate_file(f)
        if errors:
            invalid += 1
            typer.secho(f"INVALID  {f}", fg=typer.colors.RED)
            for e in errors:
                typer.echo(f"           - {e}")
        else:
            typer.secho(f"valid    {f}", fg=typer.colors.GREEN)

    valid = len(files) - invalid
    typer.echo("")
    fg = typer.colors.RED if invalid else typer.colors.GREEN
    typer.secho(f"{valid} valid, {invalid} invalid", fg=fg)
    if invalid:
        raise typer.Exit(code=1)


@app.command()
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview the plan, write nothing (== status).")
    ] = False,
    force_local: Annotated[
        Path | None,
        typer.Option("--force-local", help="Resolve a file's collision by taking local (push)."),
    ] = None,
    force_remote: Annotated[
        Path | None,
        typer.Option("--force-remote", help="Resolve a file's collision by taking remote (pull)."),
    ] = None,
) -> None:
    """Reconcile the workspace with Ghostwriter — push/pull/collision derived per record.

    Bootstraps on first run. Direction isn't chosen: a locally-edited record pushes, a
    remote-changed one pulls, and a record changed on both sides is surfaced (never
    overwritten). Every remote write is snapshot-backed.
    """
    root = Path.cwd()
    boot = bootstrap_workspace(root)
    creds = load_creds(root)
    try:
        creds.require_ghostwriter()
    except MissingCreds as e:
        if boot.env_created:
            typer.secho(f"Scaffolded workspace + wrote {boot.env_path}", fg=typer.colors.GREEN)
        typer.secho(str(e), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from None

    fl = {force_local.resolve()} if force_local else set()
    fr = {force_remote.resolve()} if force_remote else set()
    with _workspace_lock(root):  # one sync at a time per workspace (GW has no compare-and-swap)
        with GhostwriterClient(creds) as client:
            result = run_sync(root, client, dry_run=dry_run, force_local=fl, force_remote=fr)
        _print_sync_summary(result, dry_run=dry_run)
        bad = bool(
            result.collisions or result.invalid or result.mass_change_blocked or result.errors
        )

        if creds.bs_url and creds.bs_token_id and creds.bs_token_secret:
            with BookStackClient(creds) as bs:
                m = sync_methodology(root, bs, dry_run=dry_run, force_local=fl, force_remote=fr)
            _print_meth_summary(m, dry_run=dry_run)
            bad = bad or bool(
                m.collisions or m.invalid or m.drift or m.artifacts
                or m.mass_change_blocked or m.errors
            )

    if bad:
        raise typer.Exit(code=1)


@contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    """Serialize sync runs per workspace via an exclusive flock on .grison/lock."""
    lock_path = root / ".grison" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.secho(
                "another grison sync is already running in this workspace (.grison/lock held)",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1) from None
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _print_meth_summary(m: MethResult, *, dry_run: bool) -> None:
    tense = "would " if dry_run else ""
    typer.secho(
        f"methodology: {tense}pull {len(m.pulled)}, {tense}push {len(m.pushed)}, "
        f"{tense}create {len(m.created)}  ({len(m.unchanged)} clean, {len(m.repaired)} repaired)",
        fg=typer.colors.GREEN,
    )
    if m.snapshot_dir:
        typer.echo(f"snapshot: {m.snapshot_dir}")
    if m.mass_change_blocked:
        typer.secho(
            "MASS-CHANGE GUARD tripped on methodology — writes withheld.", fg=typer.colors.RED
        )
    for p, why in m.drift:
        typer.secho(f"structure-drift  {p}: {why}", fg=typer.colors.RED)
    for p, what in m.artifacts:
        typer.secho(f"artifact  {p}: {what}", fg=typer.colors.RED)
    if m.collisions:
        typer.secho(
            f"{len(m.collisions)} collision(s) — hand-merge then --force-*:", fg=typer.colors.RED
        )
        for p in m.collisions:
            typer.echo(f"  ! {p}")
    for p in m.invalid:
        typer.secho(f"broken link  {p}", fg=typer.colors.RED)
    for p, reason in m.skipped:
        typer.secho(f"skipped  {p}: {reason}", fg=typer.colors.YELLOW)
    for e in m.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)


def _print_sync_summary(result: SyncResult, *, dry_run: bool) -> None:
    tense = "would " if dry_run else ""
    ev = ""
    if result.evidence_up or result.evidence_down or result.evidence_deleted:
        ev = f"  [evidence ↑{result.evidence_up} ↓{result.evidence_down}"
        if result.evidence_deleted:
            ev += f" ✕{result.evidence_deleted}"
        ev += "]"
    typer.secho(
        f"{tense}pull {len(result.pulled)}, {tense}push {len(result.pushed)}, "
        f"{tense}insert {len(result.inserted)}  ({len(result.unchanged)} clean, "
        f"{len(result.repaired)} repaired){ev}",
        fg=typer.colors.GREEN,
    )
    if result.snapshot_dir:
        typer.echo(f"snapshot: {result.snapshot_dir}")
    if result.mass_change_blocked:
        typer.secho(
            "MASS-CHANGE GUARD tripped — remote writes withheld. Re-run a narrower path "
            "or confirm with a targeted sync.",
            fg=typer.colors.RED,
        )
    if result.collisions:
        typer.secho(
            f"{len(result.collisions)} collision(s) — hand-merge then --force-local/-remote:",
            fg=typer.colors.RED,
        )
        for p in result.collisions:
            typer.echo(f"  ! {p}  (remote at {p.with_suffix('.remote.md').name})")
    if result.invalid:
        typer.secho(f"{len(result.invalid)} broken link(s) (id set, no sync base) — re-link with "
                    "--force-remote/--force-local:", fg=typer.colors.RED)
        for p in result.invalid:
            typer.echo(f"  ? {p}")
    for p, reason in result.skipped:
        typer.secho(f"skipped  {p}: {reason}", fg=typer.colors.YELLOW)
    for e in result.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)


def _resolve_md(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob("*.md")))
        elif p.is_file():
            files.append(p)
    return files


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
        typer.echo(f"{verb} {len(sink.written)} → {out_dir}  ({len(sink.unchanged)} unchanged)")

    for path, reason in summary.skipped_files:
        typer.secho(f"skipped  {path.name}: {reason}", fg=typer.colors.YELLOW)

    if summary.warnings:
        typer.secho(f"{len(summary.warnings)} warning(s):", fg=typer.colors.YELLOW)
        for w in summary.warnings:
            typer.echo(f"  - {w}")

    if summary.errors:
        typer.secho(f"{len(summary.errors)} finding(s) failed validation:", fg=typer.colors.RED)
        for e in summary.errors:
            typer.echo(f"  - {e}")


def main() -> None:
    """Console-script entry point (``grison``)."""
    app()


if __name__ == "__main__":
    main()
