"""Scanner intermediate representation (vendored from gw-import).

This is the parsers' **output type** — a plain dataclass ``Finding`` whose prose
fields are Ghostwriter HTML strings — kept minimally-diverged from the salvage
source. It is distinct from :mod:`grison.model` (the pydantic house schema);
``grison.markdown`` (Phase 4) converts this IR into a :class:`grison.model.Finding`.
"""

from __future__ import annotations

from grison.scanners.ir.finding import Finding
from grison.scanners.ir.severity import Severity, cvss_to_severity, parse_severity_filter

__all__ = ["Finding", "Severity", "cvss_to_severity", "parse_severity_filter"]
