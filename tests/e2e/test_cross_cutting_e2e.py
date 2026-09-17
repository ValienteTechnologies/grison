"""End-to-end cross-cutting scenarios that span all three sync phases, or sit outside
the reconcile engine entirely: exit-code policy, the workspace lock, opt-in git
driving (``GRISON_GIT=commit``), and first-run bootstrap.
"""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

REPORT_SCOPES = [{"name": "Internal range", "scope": "10.0.0.0/24", "description": "",
                   "disallowed": False, "requiresCaution": False}]


# ---------------------------------------------------------------------------
# Exit-code policy per phase
#
# Transcribed from grison/cli.py's sync() (read, not reproduced by import — this
# test would need updating, not silently pass, if the predicate changes):
#
#     bad = bool(phase_errors)
#     if result is not None:  (findings)
#         bad |= bool(result.collisions or result.invalid or result.corrupt
#                      or result.mass_change_blocked or result.errors)
#     if rep is not None:  (reports)
#         bad |= bool(rep.collisions or rep.mass_change_blocked or rep.errors
#                      or rep.scope_failures)
#     if m is not None:  (methodology)
#         bad |= bool(m.collisions or m.invalid or m.drift or m.artifacts
#                      or m.mass_change_blocked or m.errors)
#
# Notably: `skipped` and `warnings` never flip `bad` for ANY phase — a lone
# wysiwyg-page skip or an orphan-record skip is not itself a failure. Notably too,
# a methodology `artifacts` hit flips `bad` even when the artifact was pre-existing
# and the push was NOT refused (see test_methodology_e2e.py's grandfathering test) —
# the corruption is merely *surfaced*, but its presence alone still taints the run.
#
# Today, `grison sync`'s overall exit code is 1 in EVERY case below regardless of
# which case fires, because the findings phase fails unconditionally before any
# record is even looked at (the evidence-query production break — see
# test_findings_e2e.py) and phase_errors is always non-empty. This is exactly the
# fragmentation D6 ("one result/exit-code policy") replaces: today, three different
# `bad` predicates happen to agree only because one of them is permanently tripped.
# What each case below actually proves is that the phase's own summary line reports
# the case honestly, not that the exit code discriminates between them (it can't,
# today) — see each case's ``marker`` string in the assertion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_name",
    ["methodology_collision", "reports_scope_failure", "methodology_mass_change"],
)
def test_exit_code_is_always_1_today_but_each_failure_class_reports_honestly(
    run_grison, gw_server, bs_server, case_name,
) -> None:
    if case_name == "methodology_collision":
        book = bs_server.store.seed_book(name="Playbook")
        page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
        run_grison("sync")
        path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n\nlocal change", encoding="utf-8")
        bs_server.store.page(page["id"])["markdown"] = "# N\n\nremote change"
        marker = "collision"
    elif case_name == "reports_scope_failure":
        gw_server.store.seed_report(
            id=7, title="R", extraFields={"executive_summary": "<p>s</p>"},
            project={"codename": "OP-X", "scopes": []},
        )
        marker = "no scope defined"
    else:
        book = bs_server.store.seed_book(name="Playbook")
        for i in range(6):
            bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"# {i}")
        run_grison("sync")
        for i in range(6):
            p = Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md"
            p.write_text(p.read_text(encoding="utf-8") + "\n\nedit", encoding="utf-8")
        marker = "MASS-CHANGE GUARD"

    result = run_grison("sync")

    assert marker in result.output
    assert result.exit_code == 1  # always 1 today — see the module-level note above


def test_a_lone_skip_does_not_by_itself_change_the_exit_code(run_grison, bs_server) -> None:
    """A skip (wysiwyg page, orphaned record, …) is not, on its own, a failure —
    it doesn't appear in any of the three `bad` predicates transcribed above."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(
        book_id=book["id"], name="WYSIWYG", editor="wysiwyg", markdown="",
        raw_html="<p>rich</p>",
    )

    result = run_grison("sync")

    assert "wysiwyg" in result.output.lower()
    # methodology's own line shows nothing blocked — 0 collisions/drift/artifacts, and
    # methodology never appears in phase_errors; only the ever-present findings-phase
    # break makes the overall exit code 1 (see the module note above).
    assert "methodology: pull 0, push 0, create 0  (0 clean, 0 repaired)" in result.output


# ---------------------------------------------------------------------------
# Workspace lock
# ---------------------------------------------------------------------------


def test_second_concurrent_sync_refuses_while_the_lock_is_held(run_grison, workspace) -> None:
    lock_dir = workspace / ".grison"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / "lock").open("w", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_grison("sync")
        assert result.exit_code == 1
        assert "another grison sync is already running" in result.output
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


# ---------------------------------------------------------------------------
# Opt-in git driving (GRISON_GIT=commit)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _git("init", cwd=path)
    _git("config", "user.name", "grison-test", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)


def _log_subjects(path: Path) -> list[str]:
    r = _git("log", "--pretty=%s", cwd=path)
    return r.stdout.splitlines() if r.returncode == 0 else []


def test_git_commit_checkpoints_and_commits_after_a_real_sync(
    run_grison, workspace, bs_server, monkeypatch,
) -> None:
    _init_repo(workspace)
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync")

    subjects = _log_subjects(workspace)
    assert any(s == "grison: pre-sync checkpoint" for s in subjects)
    assert any(s.startswith("grison: sync") for s in subjects)
    assert _git("status", "--porcelain", cwd=workspace).stdout.strip() == ""  # fully committed


def test_git_commit_dry_run_commits_nothing(run_grison, workspace, bs_server, monkeypatch) -> None:
    _init_repo(workspace)
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync", "--dry-run")

    r = _git("rev-list", "--all", "--count", cwd=workspace)
    assert r.stdout.strip() == "0"  # not a single commit was made


def test_git_staging_is_scoped_to_the_workspace_root(
    run_grison, workspace, bs_server, monkeypatch,
) -> None:
    """The workspace may be a subdirectory of a larger repo — staging must never
    reach outside it (``git add -A -- .`` with ``-C <workspace root>``)."""
    outer = workspace.parent
    _init_repo(outer)
    (outer / "unrelated-outer-file.txt").write_text("do not touch\n", encoding="utf-8")
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync")

    status = _git("status", "--porcelain", cwd=outer).stdout
    assert "unrelated-outer-file.txt" in status  # still untracked — never staged
    subjects = _log_subjects(outer)
    assert any(s.startswith("grison: sync") for s in subjects)  # the workspace's own commit landed


# ---------------------------------------------------------------------------
# CLAUDE.md scaffold + first-run bootstrap
# ---------------------------------------------------------------------------


def test_claude_md_scaffolded_on_first_bootstrap_and_never_clobbered(run_grison, bs_server) -> None:
    claude_md = Path.cwd() / "CLAUDE.md"
    assert not claude_md.exists()

    run_grison("sync")

    assert claude_md.exists()
    assert "grison workspace" in claude_md.read_text(encoding="utf-8")

    claude_md.write_text("# Hand-edited operator notes\n", encoding="utf-8")
    run_grison("sync")

    assert claude_md.read_text(encoding="utf-8") == "# Hand-edited operator notes\n"


def test_first_run_bootstrap_creates_env_template_and_exits_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``run_grison``/``workspace`` fixture here on purpose — those pre-seed
    ``.grison/env`` with working fake creds; this test needs a genuinely fresh
    directory to exercise the first-run bootstrap path (which bails out before any
    Ghostwriter/BookStack client is ever constructed, so no fake transport is
    needed)."""
    root = tmp_path / "fresh-workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    for key in ("GRISON_GW_URL", "GRISON_GW_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    import grison.cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, ["sync"])

    env_path = root / ".grison" / "env"
    assert result.exit_code == 1
    assert env_path.exists()
    assert "GRISON_GW_URL=" in env_path.read_text(encoding="utf-8")
    assert oct(env_path.stat().st_mode)[-3:] == "600"
    assert "missing Ghostwriter credentials" in result.output
    assert "Fill them into .grison/env" in result.output
