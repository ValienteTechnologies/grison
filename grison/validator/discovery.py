"""Find the workspace root from any directory inside it — the same discovery a git
command does for ``.git/``, so ``grison validate`` (and later ``grison sync``/the
post-edit hook) works from any subdirectory, not just the workspace root.
"""

from __future__ import annotations

from pathlib import Path

from grison.validator.errors import WorkspaceNotFound


def find_workspace_root(start: Path) -> Path:
    """Walk up from ``start`` to the nearest ancestor (inclusive) containing
    ``.grison/``. Raises :class:`WorkspaceNotFound` if none does."""
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".grison").is_dir():
            return candidate
    raise WorkspaceNotFound(
        f"no grison workspace found: no .grison/ directory in {start} or any parent "
        "directory"
    )
