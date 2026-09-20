"""Exceptions ``grison.validator`` raises on its own initiative — never for a
malformed document (that always becomes a :class:`~grison.validator.registry.Failure`
instead; see ``grison/validator/core.py``'s module docstring), only for a call the
validator genuinely cannot service: no workspace found, or a ``paths=`` argument that
doesn't name a real, in-workspace, validated location. The sync engine and the
post-edit hook call :func:`~grison.validator.core.validate_workspace` directly, so
these need to be real, catchable exceptions — not a silent empty result a caller could
mistake for "nothing wrong here".
"""

from __future__ import annotations

from grison.errors import GrisonError


class WorkspaceNotFound(GrisonError, ValueError):
    """No ``.grison/`` directory found walking up from the start path."""


class ValidationScopeError(GrisonError, ValueError):
    """A path passed as ``validate_workspace(..., paths=[...])`` doesn't resolve to a
    validated location: it's outside the workspace, it doesn't exist (and
    ``deleted_ok`` doesn't rescue it), or it sits somewhere never validated (inside
    ``.grison/``, or any path not under ``findings/``/``methodology/``)."""
