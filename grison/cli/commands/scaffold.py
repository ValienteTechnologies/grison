"""``grison scaffold`` and the ``grison hook`` sub-app — see :mod:`grison.cli` for
the CLI itself.

Everything an agent reads in a workspace (CLAUDE.md, .claude/settings.json,
.grison/SPEC.md, .grison/templates/) is generated from the same definitions
grison.validator enforces — see grison/scaffold/ for the generators and
grison/remote/bootstrap.py for where this runs automatically on every sync/parse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from grison.cli import app
from grison.cli.guards import _guarded
from grison.fsio import ensure_private_dir
from grison.remote.creds import load_settings
from grison.workspace import bootstrap_tree


@app.command(help="Regenerate the files grison manages in the workspace.")
@_guarded
def scaffold(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Rewrite the managed files from scratch instead of only adding what is missing.",
        ),
    ] = False,
) -> None:
    # User-facing behavior is in --help/README's ## Commands; nothing dev-only to
    # add here.
    from grison.scaffold import scaffold_workspace as _scaffold_workspace

    root = Path.cwd()
    bootstrap_tree(root)
    ensure_private_dir(root / ".grison")
    settings = load_settings(root)
    result = _scaffold_workspace(root, force=force, settings=settings)

    if result.spec_written:
        typer.echo("wrote .grison/SPEC.md")
    for name in result.templates_written:
        typer.echo(f"wrote .grison/templates/{name}")
    if result.terms_written:
        typer.echo("wrote .grison/terms.txt")
    if result.settings_updated:
        typer.echo("updated .claude/settings.json")
    typer.echo(f"CLAUDE.md: {result.claude_md_status}")
    pc = result.precommit
    if pc is not None:
        typer.secho(
            f"pre-commit hook: {pc.reason}", fg=typer.colors.YELLOW if not pc.installed else None
        )
        if pc.instructions:
            typer.echo(pc.instructions)


hook_app = typer.Typer(help="Hooks grison scaffolds into .claude/settings.json.")
app.add_typer(hook_app, name="hook")


@hook_app.command(
    "post-edit",
    help="Post-edit hook body: validates only the edited file and prints feedback. Always exits 0.",
)
def hook_post_edit() -> None:
    """Body lives in grison/scaffold/hook.py. Always exits 0 — a PostToolUse hook
    cannot block a tool call that already ran, so this only ever informs."""
    from grison.scaffold.hook import main as _hook_main

    raise typer.Exit(code=_hook_main())
