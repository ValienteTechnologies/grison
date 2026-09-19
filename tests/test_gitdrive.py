"""Unit tests for grison.gitdrive — the opt-in git-driving helper (GRISON_GIT=commit).

Every test drives a real, throwaway git repo under tmp_path via subprocess (no
GitPython, matching the module itself) with a local user.name/user.email so commits
work headless in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from grison import gitdrive


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "grison-test"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)


def _log_subjects(path: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=path, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def test_is_repo_true_inside_git_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert gitdrive.is_repo(tmp_path) is True


def test_is_repo_false_for_plain_directory(tmp_path: Path) -> None:
    assert gitdrive.is_repo(tmp_path) is False


def test_is_repo_false_when_git_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert gitdrive.is_repo(tmp_path) is False  # never raises — missing binary is "not a repo"


def test_is_dirty_false_on_clean_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    _commit_all(tmp_path, "seed")
    assert gitdrive.is_dirty(tmp_path) is False


def test_is_dirty_true_with_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    assert gitdrive.is_dirty(tmp_path) is True


def test_commit_stages_and_commits_dirty_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    made = gitdrive.commit(tmp_path, "grison: test commit")
    assert made is True
    assert _log_subjects(tmp_path) == ["grison: test commit"]
    assert gitdrive.is_dirty(tmp_path) is False


def test_commit_is_noop_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    assert gitdrive.commit(tmp_path, "first") is True
    assert gitdrive.commit(tmp_path, "second") is False  # nothing changed since — no-op
    assert _log_subjects(tmp_path) == ["first"]


def test_commit_scoped_to_workspace_root(tmp_path: Path) -> None:
    """The workspace may be a subdirectory of a larger repo — staging/commit must
    never touch files outside its root."""
    _init_repo(tmp_path)
    (tmp_path / "outside.txt").write_text("outside")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside")

    assert gitdrive.commit(workspace, "grison: scoped commit") is True

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert "inside.txt" not in status  # committed
    assert "outside.txt" in status  # still untracked — never staged

    show = subprocess.run(
        ["git", "show", "--stat", "--pretty=", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "outside.txt" not in show
    assert "inside.txt" in show


def test_commit_raises_on_stuck_index_lock(tmp_path: Path) -> None:
    """A stale .git/index.lock (another git process, a crashed run) is a common
    real-world git failure — must surface as GitDriveError, not crash oddly."""
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / ".git" / "index.lock").write_text("")
    with pytest.raises(gitdrive.GitDriveError):
        gitdrive.commit(tmp_path, "should fail")


def test_commit_raises_when_git_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(gitdrive.GitDriveError):
        gitdrive.commit(tmp_path, "should fail")


def test_commit_raises_on_non_repo(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(gitdrive.GitDriveError):
        gitdrive.commit(tmp_path, "should fail")


# --- validation gate (brief D11 item 6) ---------------------------------------------


def _bad_finding_text() -> str:
    return "---\nseverity: not-a-real-severity\nfinding_type: web\n---\n# x\n"


def _good_finding_text() -> str:
    return (
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )


def test_commit_refuses_an_invalid_workspace_and_leaves_it_dirty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "findings" / "library").mkdir(parents=True)
    (tmp_path / "findings" / "library" / "bad.md").write_text(_bad_finding_text())

    with pytest.raises(gitdrive.GitDriveValidationError, match="FND-|commit blocked"):
        gitdrive.commit(tmp_path, "should be refused")

    assert gitdrive.is_dirty(tmp_path) is True  # nothing was staged or committed
    # no commit exists at all yet — `git log` itself fails on a repo with zero commits,
    # which is exactly the point: nothing was ever committed.
    rev_count = subprocess.run(
        ["git", "rev-list", "--all", "--count"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert rev_count == "0"


def test_commit_succeeds_on_a_valid_workspace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "findings" / "library").mkdir(parents=True)
    (tmp_path / "findings" / "library" / "good.md").write_text(_good_finding_text())

    assert gitdrive.commit(tmp_path, "clean finding") is True
    assert _log_subjects(tmp_path) == ["clean finding"]


def test_validate_false_skips_the_gate(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "findings" / "library").mkdir(parents=True)
    (tmp_path / "findings" / "library" / "bad.md").write_text(_bad_finding_text())

    assert gitdrive.commit(tmp_path, "unvalidated", validate=False) is True
    assert _log_subjects(tmp_path) == ["unvalidated"]


def test_successful_commit_sets_the_precommit_skip_var(tmp_path: Path) -> None:
    """The actual `git commit` subprocess must see GRISON_SKIP_PRECOMMIT_VALIDATE=1,
    so a scaffolded pre-commit hook doesn't re-run the same whole-workspace validate
    a second time (see grison/scaffold/precommit.py)."""
    _init_repo(tmp_path)
    (tmp_path / "findings" / "library").mkdir(parents=True)
    (tmp_path / "findings" / "library" / "good.md").write_text(_good_finding_text())

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        'if [ -n "$GRISON_SKIP_PRECOMMIT_VALIDATE" ]; then exit 0; fi\n'
        "echo 'hook ran without the skip var' >&2\nexit 1\n"
    )
    hook.chmod(0o755)

    assert gitdrive.commit(tmp_path, "should skip the hook body") is True
    assert _log_subjects(tmp_path) == ["should skip the hook body"]
