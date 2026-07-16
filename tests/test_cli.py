"""Phase-6 CLI tests: parse fills findings/inbox/, status validates it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison.cli import app

_FIX = Path(__file__).parent / "fixtures" / "scanners"
_runner = CliRunner()
_GW_CREDS_VARS = (
    "GRISON_GW_URL", "GRISON_GW_TOKEN", "GRISON_CF_CLIENT_ID", "GRISON_CF_CLIENT_SECRET",
)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "grison-test"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def _log_subjects(path: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=path, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()  # newest first


def _rev_count(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _set_fake_ghostwriter_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRISON_GW_URL", "http://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")


def _stub_sync_phases(monkeypatch: pytest.MonkeyPatch, run_sync=None, sync_reports=None) -> None:
    import grison.cli as cli_mod
    from grison.remote.reports import ReportResult
    from grison.remote.sync import SyncResult

    monkeypatch.setattr(cli_mod, "run_sync", run_sync or (lambda *a, **k: SyncResult()))
    monkeypatch.setattr(cli_mod, "sync_reports", sync_reports or (lambda *a, **k: ReportResult()))


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
    from grison.remote.reports import ReportResult
    from grison.remote.sync import SyncResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "http://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")

    def fake_run_sync(
        root, client, *, dry_run=False, force_local=None, force_remote=None, on_event=None
    ):
        return SyncResult(errors=["findings/library/bad.md: boom"])

    def fake_sync_reports(
        root, client, *, dry_run=False, force_local=None, force_remote=None, on_event=None
    ):
        return ReportResult()

    monkeypatch.setattr(cli_mod, "run_sync", fake_run_sync)
    monkeypatch.setattr(cli_mod, "sync_reports", fake_sync_reports)
    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1
    assert "boom" in r.output


def test_sync_warnings_alone_do_not_flip_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean batch that only carries non-fatal warnings (a canonicalized construct, a
    recomputed cvss score) must print them dimmed but still exit 0 — warnings are
    visibility, not a failure signal."""
    import grison.cli as cli_mod
    from grison.remote.reports import ReportResult
    from grison.remote.sync import SyncResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "http://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")

    def fake_run_sync(
        root, client, *, dry_run=False, force_local=None, force_remote=None, on_event=None
    ):
        return SyncResult(warnings=["findings/library/f.md: cvss score 5.0 disagreed..."])

    def fake_sync_reports(
        root, client, *, dry_run=False, force_local=None, force_remote=None, on_event=None
    ):
        return ReportResult()

    monkeypatch.setattr(cli_mod, "run_sync", fake_run_sync)
    monkeypatch.setattr(cli_mod, "sync_reports", fake_sync_reports)
    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0
    assert "warning:" in r.output and "cvss score" in r.output


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


# --- GRISON_GIT git-driving (Feature B) --------------------------------------------


def test_sync_git_driving_commits_checkpoint_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grison.remote.sync import SyncResult

    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    (tmp_path / "pre.txt").write_text("dirty before sync even starts\n")

    def fake_run_sync(root, client, **kwargs):
        (root / "findings" / "library" / "new.md").write_text("---\n---\n# x\n")
        return SyncResult(pulled=[Path("a.md")], pushed=[Path("b.md")])

    _stub_sync_phases(monkeypatch, run_sync=fake_run_sync)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output

    subjects = _log_subjects(tmp_path)
    assert subjects[0].startswith("grison: sync (")
    assert "findings: pull 1 push 1" in subjects[0]
    assert subjects[1] == "grison: pre-sync checkpoint"
    assert subjects[2] == "seed"
    assert not subprocess.run(  # tree clean after both checkpoints landed
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_sync_git_driving_notes_failures_in_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grison.remote.sync import SyncResult

    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    def fake_run_sync(root, client, **kwargs):
        (root / "findings" / "library" / "new.md").write_text("---\n---\n# x\n")
        return SyncResult(errors=["boom"])

    _stub_sync_phases(monkeypatch, run_sync=fake_run_sync)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1  # the phase error still fails the command

    subjects = _log_subjects(tmp_path)
    assert subjects[0].startswith("grison: sync (with failures")  # but state was still captured


def test_sync_git_driving_scoped_to_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace may be a subdir of a larger repo — grison must never stage or
    commit files outside it."""
    _init_repo(tmp_path)
    (tmp_path / "outside.txt").write_text("outside\n")
    workspace = tmp_path / "engagement"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert "outside.txt" in status  # untouched, still dirty
    show = subprocess.run(
        ["git", "show", "--stat", "--pretty=", "HEAD"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert "outside.txt" not in show


def test_sync_git_driving_silent_when_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # never a git repo
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "git" not in r.output.lower()  # detection is the feature: silent, not a warning
    assert not (tmp_path / ".git").exists()  # never inits one either


def test_sync_git_driving_disabled_makes_no_git_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grison.gitdrive as gitdrive_mod

    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.delenv("GRISON_GIT", raising=False)
    _init_repo(tmp_path)  # it IS a repo — but the setting is off, so it must be ignored
    calls: list[str] = []
    monkeypatch.setattr(gitdrive_mod, "is_repo", lambda root: calls.append("is_repo") or True)
    monkeypatch.setattr(gitdrive_mod, "commit", lambda root, msg: calls.append("commit") or True)
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert calls == []


def test_sync_dry_run_never_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted before the dry-run\n")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"  # only the seed commit


def test_sync_git_failure_warns_not_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x\n")
    (tmp_path / ".git" / "index.lock").write_text("")  # simulate a stuck git process
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output  # grison's outcome never depends on git state
    assert "git:" in r.output


def test_parse_commits_when_git_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0, r.output
    assert _log_subjects(tmp_path)[0].startswith("grison: parse burp")


def test_parse_no_commit_when_git_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRISON_GIT", raising=False)
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"  # only the seed commit


def test_parse_dry_run_never_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml"), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"


# --- CLAUDE.md scaffold (Feature C) -------------------------------------------------


def test_sync_prints_claude_md_scaffold_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in _GW_CREDS_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRISON_CLAUDE_MD", raising=False)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1  # still fails on missing creds
    assert "Scaffolded workspace + wrote CLAUDE.md" in r.output
    assert (tmp_path / "CLAUDE.md").exists()


def test_sync_claude_md_off_suppresses_scaffold_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in _GW_CREDS_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRISON_CLAUDE_MD", "off")

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1
    assert "CLAUDE.md" not in r.output
    assert not (tmp_path / "CLAUDE.md").exists()
