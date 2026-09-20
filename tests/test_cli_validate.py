"""``grison validate`` and the top-level GrisonError handler (brief item 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison import manifest as manifest_mod
from grison.cli import app

_runner = CliRunner()


def test_validate_quiet_on_empty_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert result.output == ""


def test_validate_reports_one_line_per_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    lib = tmp_path / "findings" / "library"
    lib.mkdir(parents=True)
    (lib / "bad.md").write_text(
        "---\nseverity: nope\nfinding_type: web\n---\n# T\n\n"
        "## Description\n\nd\n\n## Impact\n\ni\n\n## Mitigation\n\nm\n\n"
        "## Replication Steps\n\nr\n\n## References\n\nref\n"
    )
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "FND-003" in result.output
    assert "findings/library/bad.md" in result.output
    # format: path[:line]: RULE-ID message — fix
    assert " — " in result.output


def test_validate_json_output_is_stable_and_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    lib = tmp_path / "findings" / "library"
    lib.mkdir(parents=True)
    (lib / "bad.md").write_text("not a document at all\n")
    result = _runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert isinstance(data, list) and len(data) >= 1
    entry = data[0]
    assert set(entry) == {"rule_id", "path", "line", "message", "fix"}
    assert entry["rule_id"].startswith("FND-")


def test_validate_single_path_narrows_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    lib = tmp_path / "findings" / "library"
    lib.mkdir(parents=True)
    good = """---
severity: high
finding_type: web
---
# Good

## Description

d

## Impact

i

## Mitigation

m

## Replication Steps

r

## References

ref
"""
    (lib / "good.md").write_text(good)
    (lib / "bad.md").write_text("---\nseverity: nope\nfinding_type: web\n---\n# T\n")
    result = _runner.invoke(app, ["validate", "findings/library/good.md"])
    assert result.exit_code == 0
    assert result.output == ""


def test_validate_finds_workspace_root_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    sub = tmp_path / "findings" / "reports" / "acme"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert result.output == ""


def test_validate_no_workspace_found_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .grison/ anywhere
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 2
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output


def test_validate_dot_and_nonexistent_path_never_false_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact bug report: `grison validate .` and `grison validate nope.md` must
    never exit 0/print nothing when nothing was actually checked."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    lib = tmp_path / "findings" / "library"
    lib.mkdir(parents=True)
    (lib / "bad.md").write_text("---\nseverity: nope\nfinding_type: web\n---\n# T\n")

    dot_result = _runner.invoke(app, ["validate", "."])
    assert dot_result.exit_code == 1
    assert "bad.md" in dot_result.output

    nope_result = _runner.invoke(app, ["validate", "nope.md"])
    assert nope_result.exit_code == 2
    assert nope_result.output.startswith("error: ")
    assert "no such path" in nope_result.output


def test_validate_directory_argument_validates_everything_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    lib = tmp_path / "findings" / "library"
    lib.mkdir(parents=True)
    (lib / "bad.md").write_text("---\nseverity: nope\nfinding_type: web\n---\n# T\n")
    result = _runner.invoke(app, ["validate", "findings"])
    assert result.exit_code == 1
    assert "bad.md" in result.output


def test_validate_deleted_ok_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)
    reports = tmp_path / "findings" / "reports" / "acme"
    reports.mkdir(parents=True)

    without_flag = _runner.invoke(app, ["validate", "findings/reports/acme/gone.md"])
    assert without_flag.exit_code == 2

    with_flag = _runner.invoke(app, ["validate", "--deleted-ok", "findings/reports/acme/gone.md"])
    assert with_flag.exit_code == 0


def test_validate_exit_2_on_internal_grisonerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 (usage/internal error) is distinct from exit 1 (real document
    failures) — a pre-commit hook must be able to tell "the validator could not run"
    apart from "it ran and found problems"."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)

    import grison.cli as cli_mod
    from grison.errors import GrisonError

    def _boom(root: Path, *, paths: list[Path] | None = None) -> list[object]:
        raise GrisonError("could not load the rule registry")

    monkeypatch.setattr(cli_mod, "validate_workspace", _boom)
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 2
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output


def test_validate_exit_2_on_unexpected_internal_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a non-GrisonError internal bug must exit 2 with a message, never a raw
    traceback and never exit 0 (which would look like "workspace is clean")."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    manifest_mod.write(tmp_path)

    import grison.cli as cli_mod

    def _boom(root: Path, *, paths: list[Path] | None = None) -> list[object]:
        raise KeyError("unexpected")

    monkeypatch.setattr(cli_mod, "validate_workspace", _boom)
    result = _runner.invoke(app, ["validate"])
    assert result.exit_code == 2
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output


def test_grisonerror_reaches_top_as_one_plain_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brief item 4: GRISON_GW_URL=http://… must never produce a raw traceback."""
    monkeypatch.chdir(tmp_path)
    grison_dir = tmp_path / ".grison"
    grison_dir.mkdir()
    (grison_dir / "env").write_text("GRISON_GW_URL=http://insecure.example\nGRISON_GW_TOKEN=tok\n")
    result = _runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 1
    assert result.output.startswith("error: ")
    assert "https" in result.output
    assert "Traceback" not in result.output
