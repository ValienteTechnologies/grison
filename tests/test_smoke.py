"""Phase-1 smoke tests — the package imports and the CLI help renders."""

from __future__ import annotations

from importlib.metadata import version

from typer.testing import CliRunner

from grison import __version__
from grison.cli import app


def test_version() -> None:
    assert __version__ == version("grison")


def test_help_renders() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "grison" in result.stdout.lower()
