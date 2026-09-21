"""The per-phase isolation wrapper ``grison.cli.commands.sync``/``status`` run every
sync phase through — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import typer

_T = TypeVar("_T")


def _run_phase(name: str, fn: Callable[[], _T], phase_errors: list[str]) -> _T | None:
    """Run one sync phase (findings / reports / methodology) in isolation. An exception
    here is recorded in ``phase_errors`` and printed, but never propagated — the phases
    that follow still get to run, and the run exits nonzero at the end regardless."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — isolate this phase, subsequent phases still run
        msg = f"{name} sync failed: {e}"
        phase_errors.append(msg)
        typer.secho(msg, fg=typer.colors.RED)
        return None
