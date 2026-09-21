"""``grison validate`` — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from grison.cli import app
from grison.cli.guards import _guarded
from grison.errors import GrisonError
from grison.validator import find_workspace_root, validate_workspace


@app.command(help="Check every document against the workspace format (offline).")
@_guarded
def validate(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Only these files or dirs (default: the whole workspace)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="JSON output."),
    ] = False,
    deleted_ok: Annotated[
        bool,
        typer.Option(
            "--deleted-ok",
            help="Accept a path that no longer exists (for hooks running after a delete).",
        ),
    ] = False,
) -> None:
    """Dev notes (user-facing behavior is in ``--help``/README's ``## Commands``):

    The 1-vs-2 exit-code split is deliberate — a pre-commit hook or CI step must
    be able to tell "your documents are invalid" (1, fix the documents) apart
    from "validate itself is broken" (2, fix grison / the invocation), never both
    looking like the same failure. A ``PATHS`` entry that matches nothing is
    class 2, never a silent 0 — see §9 of the workspace-format spec.
    """
    try:
        root = find_workspace_root(Path.cwd())
        failures = validate_workspace(root, paths=paths, deleted_ok=deleted_ok)
    except GrisonError as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    except Exception as e:  # noqa: BLE001 — "could not run" must never look like "passed"
        typer.secho(f"error: validator failed to run: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "rule_id": f.rule_id,
                        "path": f.path,
                        "line": f.line,
                        "message": f.message,
                        "fix": f.fix,
                    }
                    for f in failures
                ],
                indent=2,
            )
        )
    else:
        for f in failures:
            loc = f"{f.path}:{f.line}" if f.line is not None else f.path
            typer.echo(f"{loc}: {f.rule_id} {f.message} — {f.fix}")
    if failures:
        raise typer.Exit(code=1)
