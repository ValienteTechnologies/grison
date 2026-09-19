"""A git ``pre-commit`` hook that runs whole-workspace ``grison validate`` and blocks
the commit on failure (brief D11's third enforcement point).

Uses ``git rev-parse --git-path hooks`` to find the REAL hooks directory — this
already accounts for ``core.hooksPath`` and for a worktree's own git-common-dir, so
this module never needs to special-case either: if hooks live somewhere unusual, this
is exactly where a hook file placed here would be picked up. An existing ``pre-commit``
file that isn't grison's own (no marker line) is never overwritten — the exact text to
add by hand is printed instead.
"""

from __future__ import annotations

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from grison import gitdrive
from grison.fsio import atomic_write_text

_MARKER = "# grison-managed pre-commit hook (see `grison scaffold`) — do not hand-edit"
_TIMEOUT = 30


@dataclass(frozen=True)
class PrecommitResult:
    installed: bool
    reason: str
    hook_path: Path | None = None
    instructions: str | None = None  # set when a foreign hook was left untouched


def _git_hooks_dir(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else root / path


def render_hook(root: Path) -> str:
    """The hook script's exact text. ``GRISON_SKIP_PRECOMMIT_VALIDATE`` is set by
    :func:`grison.gitdrive.commit` around grison's own commits (``GRISON_GIT=commit``)
    so a sync/parse-triggered commit — which already validated in Python before ever
    calling ``git commit`` — doesn't pay for a second, redundant whole-workspace
    validate; see that module's docstring for the other half of this contract."""
    return f"""\
#!/bin/sh
{_MARKER}
if [ -n "$GRISON_SKIP_PRECOMMIT_VALIDATE" ]; then
    exit 0
fi
cd {_sh_quote(str(root))} || exit 1
grison validate
code=$?
if [ "$code" -ne 0 ]; then
    echo "grison: commit blocked — fix the failures above" >&2
    echo "(bypass with 'git commit --no-verify', not recommended)" >&2
fi
exit $code
"""


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def install_precommit_hook(root: Path, *, force: bool = False) -> PrecommitResult:
    if not gitdrive.is_repo(root):
        return PrecommitResult(installed=False, reason="not a git repository")

    hooks_dir = _git_hooks_dir(root)
    if hooks_dir is None:
        return PrecommitResult(
            installed=False,
            reason="could not determine the git hooks directory (is git installed?)",
        )

    hook_path = hooks_dir / "pre-commit"
    content = render_hook(root)

    if hook_path.exists() and not force:
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if existing == content:
            return PrecommitResult(installed=True, reason="already installed", hook_path=hook_path)
        if _MARKER in existing:
            atomic_write_text(hook_path, content)
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return PrecommitResult(installed=True, reason="updated", hook_path=hook_path)
        instructions = (
            f"{hook_path} already exists and isn't grison's own hook — add this to it "
            "by hand instead of letting grison overwrite it:\n\n"
            + "\n".join(f"    {line}" for line in content.splitlines())
        )
        return PrecommitResult(
            installed=False,
            reason="an existing foreign pre-commit hook was left untouched",
            hook_path=hook_path,
            instructions=instructions,
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(hook_path, content)
    hook_path.chmod(0o755)
    return PrecommitResult(installed=True, reason="installed", hook_path=hook_path)
