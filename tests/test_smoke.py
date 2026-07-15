"""Phase-1 smoke tests — the package imports and the CLI help renders."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

from grison import __version__
from grison.cli import app


def test_version() -> None:
    # VERSION (repo root) is the single source of truth: hatchling reads it at
    # build/install time, __version__ reads the installed metadata. A mismatch
    # means the editable install is stale (re-run `uv sync`) or packaging broke.
    ssot = (Path(__file__).parents[1] / "VERSION").read_text().strip()
    assert __version__ == version("grison") == ssot


def test_help_renders() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "grison" in result.stdout.lower()
