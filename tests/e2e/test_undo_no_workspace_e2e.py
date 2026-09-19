"""``grison undo`` outside any workspace is "could not run" — exit 2, like
``status``/``validate``, never the generic exit 1."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from grison.cli import app


def test_undo_outside_a_workspace_exits_2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["undo", "--list"])
    assert result.exit_code == 2, result.output
    assert "no grison workspace found" in (result.output + str(result.stderr_bytes or b""))
