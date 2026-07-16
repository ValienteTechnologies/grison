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
        cwd=path, check=True, capture_output=True,
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
        cwd=tmp_path, check=True, capture_output=True, text=True,
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
