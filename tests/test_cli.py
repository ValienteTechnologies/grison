"""Phase-6 CLI tests: parse fills findings/inbox/, status validates it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison.cli import app

_FIX = Path(__file__).parent / "fixtures" / "scanners"
_runner = CliRunner()


def test_help_lists_verbs() -> None:
    result = _runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "parse" in result.output and "status" in result.output


def test_parse_bootstraps_and_status_reports_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans = tmp_path / "scans"
    scans.mkdir()
    for name in ("burp_sample.xml", "nessus_sample.xml", "sslyze_sample.json"):
        shutil.copy(_FIX / name, scans / name)
    monkeypatch.chdir(tmp_path)  # workspace root = tmp_path

    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 0, r.output
    assert "Parsed" in r.output
    inbox = tmp_path / "findings" / "inbox"
    assert (tmp_path / "findings" / "library").is_dir()  # full tree scaffolded
    md_files = list(inbox.glob("*.md"))
    assert md_files

    r2 = _runner.invoke(app, ["status", str(inbox)])
    assert r2.exit_code == 0, r2.output
    assert "0 invalid" in r2.output


def test_parse_skips_unrecognized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "notes.txt").write_text("not a scan\n")
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 0
    assert "skipped" in r.output and "notes.txt" in r.output


def test_parse_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml"), "--dry-run"])
    assert r.exit_code == 0
    assert "Would write" in r.output
    assert list((tmp_path / "findings" / "inbox").glob("*.md")) == []


def test_sync_exit_code_reflects_result_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that finishes with isolated per-record errors must still exit non-zero —
    a green summary line next to a swallowed error would be misleading."""
    import grison.cli as cli_mod
    from grison.remote.sync import SyncResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "http://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")

    def fake_run_sync(root, client, *, dry_run=False, force_local=None, force_remote=None):
        return SyncResult(errors=["findings/library/bad.md: boom"])

    monkeypatch.setattr(cli_mod, "run_sync", fake_run_sync)
    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1
    assert "boom" in r.output


def test_status_flags_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    # valid frontmatter/schema but a table in the body → GW whitelist violation
    bad.write_text(
        "---\ngrison:\n  tier: library\nseverity: low\nfinding_type: host\n---\n\n"
        "# Bad\n\n## Description\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    )
    r = _runner.invoke(app, ["status", str(bad)])
    assert r.exit_code == 1
    assert "INVALID" in r.output and "1 invalid" in r.output
