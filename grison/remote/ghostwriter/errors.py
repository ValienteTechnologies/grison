"""Ghostwriter-specific error types."""

from __future__ import annotations

from grison.errors import GrisonError


class GhostwriterError(GrisonError, RuntimeError):
    """Raised on a non-2xx HTTP response or a GraphQL ``errors`` payload."""
