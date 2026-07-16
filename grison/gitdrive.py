"""Opt-in git driving (``GRISON_GIT=commit``) — see :class:`grison.remote.creds.Settings`.

grison never ``init``s, pushes, branches, checks out, resets, or touches a remote — it
only checkpoints the workspace tree with plain commits, and only when the operator has
opted in *and* the workspace root already sits inside a git repo ("drive git if it
detects one"). Staging is always scoped to the workspace root (``git add -A -- .``
with ``-C root``), never the whole repo, since the workspace may be a subdirectory of a
larger one. Every git failure raises :class:`GitDriveError`; callers must catch it and
warn — grison's own outcome must never depend on git state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT = 30  # seconds — a safety net against a wedged git (e.g. an interactive hook)


class GitDriveError(RuntimeError):
    """A git operation failed. Callers must warn, never fail the command, on this."""


def is_repo(root: Path) -> bool:
    """True if ``root`` sits inside a git working tree. Never raises: a missing ``git``
    binary or a plain non-repo directory are both just "not a repo" here — detection
    itself is the feature, so this is the one check that stays silent either way."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def is_dirty(root: Path) -> bool:
    """True if the workspace subtree (``root`` and below) has uncommitted changes."""
    return bool(_run(root, ["status", "--porcelain", "--", "."]).strip())


def commit(root: Path, message: str) -> bool:
    """Stage everything under ``root`` and commit, if the tree is dirty.

    Returns True if a commit was made, False if the tree was already clean (a no-op,
    not a failure). Raises :class:`GitDriveError` on any git failure.
    """
    if not is_dirty(root):
        return False
    _run(root, ["add", "-A", "--", "."])
    _run(root, ["commit", "-m", message])
    return True


def _run(root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitDriveError(f"git {' '.join(args)} failed: {e}") from e
    if result.returncode != 0:
        raise GitDriveError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
