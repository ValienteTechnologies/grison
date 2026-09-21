"""grison CLI — three verbs: ``parse``, ``status``, ``sync``.

The path names the backend (``findings/`` ⇄ Ghostwriter, ``methodology/`` ⇄
BookStack); location decides identity; the first ``sync`` bootstraps the workspace.
``parse`` and ``status`` are offline; ``sync`` reconciles findings with Ghostwriter
and methodology with BookStack (push/pull/collision derived per record).

This package is a pure split of what used to be one ``grison/cli.py`` module: each
command lives in :mod:`grison.cli.commands`, each sync phase in
:mod:`grison.cli.phases`, and the cross-cutting helpers (client construction,
workspace guards, text rendering, JSON payloads, git-driving) in their own modules
alongside this one. This module wires them together — it defines ``app`` first,
then imports every command module so its ``@app.command()``/``@hook_app.command()``
decorators register it, then re-exports the names tests and the console-script
entry point rely on.
"""

from __future__ import annotations

from typing import Annotated

import typer

from grison.cli.clients import _make_bs_client, _make_gw_client
from grison.cli.phases.findings import FindingsPhaseResult
from grison.cli.phases.reports import ReportsPhaseResult
from grison.cli.render import _format_reasons, _print_version

__all__ = [
    "FindingsPhaseResult",
    "ReportsPhaseResult",
    "_format_reasons",
    "_make_bs_client",
    "_make_gw_client",
    "app",
    "hook_app",
    "main",
]

app: typer.Typer = typer.Typer(
    name="grison",
    help="A markdown hub between security scanners and Ghostwriter + BookStack.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    """grison — parse scanner artifacts to markdown, then status/sync with the remotes."""


# Import every command module for its side effect: each one's `@app.command()` (and,
# for `scaffold`, `@hook_app.command()`) decorator registers it onto `app` above.
# Order among them doesn't matter — Typer just accumulates registrations — but this
# must come after `app`/`_root` are defined, since every command module does
# `from grison.cli import app`.
from grison.cli.commands import parse  # noqa: E402,F401,I001
from grison.cli.commands import scaffold  # noqa: E402,F401
from grison.cli.commands import status  # noqa: E402,F401
from grison.cli.commands import sync  # noqa: E402,F401
from grison.cli.commands import undo  # noqa: E402,F401
from grison.cli.commands import validate  # noqa: E402,F401
from grison.cli.commands.scaffold import hook_app  # noqa: E402


def main() -> None:
    """Console-script entry point (``grison``)."""
    app()


if __name__ == "__main__":
    main()
