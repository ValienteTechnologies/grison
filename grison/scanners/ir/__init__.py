"""Scanner intermediate representation (vendored from gw-import).

This is the parsers' **output type** — a plain dataclass ``ScanFinding`` whose prose
fields are Ghostwriter HTML strings — kept minimally-diverged from the salvage
source. It is distinct from :mod:`grison.model` (the pydantic house schema);
``grison.markdown`` (Phase 4) converts this IR into a :class:`grison.model.Finding`.
"""

from __future__ import annotations

from grison.scanners.ir.cvss2 import cvss2_to_cvss3, ensure_cvss3_prefix
from grison.scanners.ir.cwe import normalize_cwe
from grison.scanners.ir.finding import ScanFinding
from grison.scanners.ir.severity import (
    SEVERITY_ORDER,
    Severity,
    cvss_to_severity,
    max_severity,
    parse_severity_filter,
    severity_or,
    severity_or_info,
)

__all__ = [
    "SEVERITY_ORDER",
    "ScanFinding",
    "Severity",
    "cvss2_to_cvss3",
    "cvss_to_severity",
    "ensure_cvss3_prefix",
    "max_severity",
    "normalize_cwe",
    "parse_severity_filter",
    "severity_or",
    "severity_or_info",
]
