"""Schema layer: enums and CVSS/CWE validators.

Everything downstream (markdown serialization, sinks, remote sync) depends on these
tier-agnostic pieces. The tier-agnostic ``Finding`` pydantic model that used to live in
``grison.model.finding`` (with its embedded ``grison:``/GW-ref/sync-state block) was
workspace format v1's document shape — format v2 documents carry no machine fields at
all (see :mod:`grison.formats.finding`'s ``FindingDoc``), so that model has no callers
left and was deleted with the v1→v2 migration (D3).
"""

from __future__ import annotations

from grison.model.cvss import CvssError, CvssVector, is_valid_cvss, parse_cvss
from grison.model.cwe import cwe_name, is_known_cwe, normalize_cwe
from grison.model.enums import (
    EnumDriftError,
    FindingType,
    Severity,
    check_finding_type_drift,
    check_severity_drift,
)

__all__ = [
    "CvssError",
    "CvssVector",
    "EnumDriftError",
    "FindingType",
    "Severity",
    "check_finding_type_drift",
    "check_severity_drift",
    "cwe_name",
    "is_known_cwe",
    "is_valid_cvss",
    "normalize_cwe",
    "parse_cvss",
]
