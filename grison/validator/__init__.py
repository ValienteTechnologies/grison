"""The ONE offline validator for workspace format v2.

``validate_workspace(root, *, paths=None, deleted_ok=False)`` returns every
:class:`~grison.validator.registry.Failure` for the workspace (or just the scope
``paths`` names — see ``docs/workspace-format.md`` §9). A ``paths`` entry that isn't a
real, in-workspace, validated location raises
:class:`~grison.validator.errors.ValidationScopeError` rather than silently
contributing no failures. ``find_workspace_root`` walks up from a directory to the
nearest ``.grison/`` (raising :class:`~grison.validator.errors.WorkspaceNotFound` if
there is none) — the same discovery the CLI, the sync engine, and the post-edit hook
all need before they can call ``validate_workspace`` at all.

Every rule fired here has a matching entry in :data:`~grison.validator.registry.RULES`
and a matching section in ``docs/workspace-format.md`` — see
``tests/test_spec_coverage.py``.
"""

from __future__ import annotations

from grison.validator.core import validate_workspace
from grison.validator.discovery import find_workspace_root
from grison.validator.errors import ValidationScopeError, WorkspaceNotFound
from grison.validator.registry import RULES, Failure, Rule

__all__ = [
    "RULES",
    "Failure",
    "Rule",
    "ValidationScopeError",
    "WorkspaceNotFound",
    "find_workspace_root",
    "validate_workspace",
]
