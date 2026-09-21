"""``grison parse`` — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from grison.cli import app
from grison.cli.gitdriving import _git_commit_or_warn, _scanner_label
from grison.cli.guards import _guarded, _refuse_if_format_mismatch
from grison.cli.render import _print_parse_summary
from grison.model import FindingType
from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.creds import load_settings
from grison.sinks import ParsePathNotFound, run_parse
from grison.workspace import inbox_dir


@app.command()
@_guarded
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
    """Turn scanner exports into markdown findings in findings/inbox/."""
    missing = [p for p in paths if not p.exists()]
    if missing:
        # "could not run" (a typo'd/missing path), never "ran and found nothing" —
        # exit 2, before any scaffolding/parsing/writing, the same class as
        # no-workspace/bad-creds elsewhere in this file.
        typer.secho(
            f"error: no such file or directory: {', '.join(str(p) for p in missing)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    root = Path.cwd()
    if out is None:
        # The binary scaffolds; no init. A full workspace, not just the bare
        # directory tree (spec §10 / bootstrap.py's own docstring: "a first
        # `grison sync`/`grison parse` in an empty directory yields a complete,
        # valid, self-contained workspace") — bootstrap_workspace is credential-
        # free (it only ever writes a template env, never contacts a remote), so
        # `parse` staying fully offline is unaffected. Without this, `grison parse`
        # in an empty directory left no manifest.yml/index.json behind and the very
        # next `grison validate` exited 2 "no grison workspace found".
        #
        # item 10, fix-fin1 (D13): a directory that already IS a workspace (of
        # any format) is refused outright before this scaffolding self-heal ever
        # touches it, exactly like `sync` — `parse` writing into a format-
        # mismatched workspace's findings/inbox/ would be just as wrong as
        # `sync` reconciling one.
        _refuse_if_format_mismatch(root)
        bootstrap_workspace(root)
        out_dir = inbox_dir(root)
    else:
        out_dir = out
    try:
        summary = run_parse(
            paths,
            out_dir,
            scanner=scanner,
            finding_type=finding_type,
            min_severity=min_severity,
            dry_run=dry_run,
        )
    except ParsePathNotFound as e:
        # "could not run" (a typo'd/missing path), never "ran and found nothing" —
        # exit 2, the same class as no-workspace/bad-creds elsewhere in this file.
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    _print_parse_summary(summary, out_dir, dry_run=dry_run)
    if not dry_run:
        _git_commit_or_warn(root, load_settings(root), f"grison: parse {_scanner_label(summary)}")
    if summary.errors:
        raise typer.Exit(code=1)
