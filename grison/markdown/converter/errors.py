"""The converter's one exception type."""

from __future__ import annotations

from grison.errors import GrisonError


class ConverterError(GrisonError, ValueError):
    """Raised when HTML or markdown outside the tiny closed GW vocabulary is seen."""
