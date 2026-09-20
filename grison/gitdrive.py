"""Opt-in git driving (``GRISON_GIT=commit``) — see :class:`grison.remote.creds.Settings`.

grison never ``init``s, pushes, branches, checks out, resets, or touches a remote — it
only checkpoints the workspace tree with plain commits, and only when the operator has
opted in *and* the workspace root already sits inside a git repo ("drive git if it
detects one"). Staging is always scoped to the workspace root (``git add -A -- .``
with ``-C root``), never the whole repo, since the workspace may be a subdirectory of a
larger one. Every git failure raises :class:`GitDriveError`; callers must catch it and
warn — grison's own outcome must never depend on git state.

**Validation gate (brief D11/item 6).** :func:`commit` runs the SAME
``grison validate`` every commit must pass — its own checkpoint/post-sync commits, and
a workspace's scaffolded git ``pre-commit`` hook (:mod:`grison.scaffold.precommit`),
are two callers of one check, not two independent ones. When validation fails here,
grison does not commit: the sync/parse that ran already happened, but its result stays
uncommitted for the owner to fix, exactly like a plain, unrelated hand-edit would leave
the tree dirty. To avoid re-running the (whole-workspace) validate a second time inside
the pre-commit hook this same commit is about to trigger, the actual ``git commit``
subprocess is run with ``GRISON_SKIP_PRECOMMIT_VALIDATE=1`` set — the scaffolded hook
checks that variable first and exits 0 immediately when it's set, trusting the
validation grison itself already performed a moment earlier in the same process.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from grison.errors import GrisonError
from grison.validator import validate_workspace

_TIMEOUT = 30  # seconds — a safety net against a wedged git (e.g. an interactive hook)
_SKIP_PRECOMMIT_VAR = "GRISON_SKIP_PRECOMMIT_VALIDATE"


class GitDriveError(GrisonError, RuntimeError):
    """A git operation failed. Callers must warn, never fail the command, on this."""


class GitDriveValidationError(GitDriveError):
    """:func:`commit` refused to commit an invalid workspace. Callers treat this the
    same as any other :class:`GitDriveError` (warn, never fail the command) — the
    workspace tree is left dirty, uncommitted, for the owner to fix by hand."""


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


def commit(root: Path, message: str, *, validate: bool = True) -> bool:
    """Stage everything under ``root`` and commit, if the tree is dirty.

    Returns True if a commit was made, False if the tree was already clean (a no-op,
    not a failure). Raises :class:`GitDriveError` on any git failure, and
    :class:`GitDriveValidationError` (a subclass) when ``validate`` is true (the
    default) and the workspace fails ``grison validate`` — the commit is refused
    rather than made, and nothing is staged.
    """
    if not is_dirty(root):
        return False
    if validate:
        failures = validate_workspace(root)
        if failures:
            preview = "; ".join(f"{f.rule_id} {f.path}" for f in failures[:3])
            more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
            raise GitDriveValidationError(
                f"commit blocked: {len(failures)} validation failure(s) — {preview}{more} "
                "— fix them (see `grison validate`), then commit by hand"
            )
    _run(root, ["add", "-A", "--", "."])
    # The pre-commit hook this triggers must not re-run the same whole-workspace
    # validate a second time — see the module docstring.
    _run(root, ["commit", "-m", message], env_overrides={_SKIP_PRECOMMIT_VAR: "1"})
    return True


def _run(root: Path, args: list[str], *, env_overrides: dict[str, str] | None = None) -> str:
    env = {**os.environ, **env_overrides} if env_overrides else None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitDriveError(f"git {' '.join(args)} failed: {e}") from e
    if result.returncode != 0:
        raise GitDriveError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
