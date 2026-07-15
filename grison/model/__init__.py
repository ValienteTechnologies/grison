"""Schema layer: the pydantic ``Finding``, enums, and CVSS/CWE validators.

Everything downstream (markdown serialization, sinks, remote sync) depends on
this one tier-agnostic schema.
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
from grison.model.finding import (
    Cvss,
    EvidenceGwRef,
    EvidenceItem,
    Finding,
    GrisonMeta,
    GwRef,
    SyncState,
)

__all__ = [
    "Cvss",
    "CvssError",
    "CvssVector",
    "EnumDriftError",
    "EvidenceGwRef",
    "EvidenceItem",
    "Finding",
    "FindingType",
    "GrisonMeta",
    "GwRef",
    "Severity",
    "SyncState",
    "check_finding_type_drift",
    "check_severity_drift",
    "cwe_name",
    "is_known_cwe",
    "is_valid_cvss",
    "normalize_cwe",
    "parse_cvss",
]
