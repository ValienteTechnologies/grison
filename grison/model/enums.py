"""Finding enums with their Ghostwriter lookup-table ids.

The GW ids are **derived from the enum**, never stored in frontmatter — the
markdown carries the human name (``medium``, ``web``); the id is materialized only
when talking to Ghostwriter. Both mappings come from the live GW lookup tables.

Note the severity id order is the **inverse of weight** (1 = Informational …
5 = Critical), a Ghostwriter quirk baked into the mapping below.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """Finding severity. GW ``severityId``: 1=Informational … 5=Critical."""

    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def gw_id(self) -> int:
        return _SEVERITY_GW_ID[self]

    @classmethod
    def from_gw_id(cls, gw_id: int) -> Severity:
        try:
            return _SEVERITY_BY_GW_ID[gw_id]
        except KeyError:
            raise ValueError(f"unknown GW severityId: {gw_id!r}") from None


class FindingType(StrEnum):
    """Finding type/domain. GW ``findingTypeId``: 1=Network … 7=Host."""

    NETWORK = "network"
    PHYSICAL = "physical"
    WIRELESS = "wireless"
    WEB = "web"
    MOBILE = "mobile"
    CLOUD = "cloud"
    HOST = "host"

    @property
    def gw_id(self) -> int:
        return _FINDING_TYPE_GW_ID[self]

    @classmethod
    def from_gw_id(cls, gw_id: int) -> FindingType:
        try:
            return _FINDING_TYPE_BY_GW_ID[gw_id]
        except KeyError:
            raise ValueError(f"unknown GW findingTypeId: {gw_id!r}") from None


_SEVERITY_GW_ID: dict[Severity, int] = {
    Severity.INFORMATIONAL: 1,
    Severity.LOW: 2,
    Severity.MEDIUM: 3,
    Severity.HIGH: 4,
    Severity.CRITICAL: 5,
}
_SEVERITY_BY_GW_ID: dict[int, Severity] = {v: k for k, v in _SEVERITY_GW_ID.items()}

_FINDING_TYPE_GW_ID: dict[FindingType, int] = {
    FindingType.NETWORK: 1,
    FindingType.PHYSICAL: 2,
    FindingType.WIRELESS: 3,
    FindingType.WEB: 4,
    FindingType.MOBILE: 5,
    FindingType.CLOUD: 6,
    FindingType.HOST: 7,
}
_FINDING_TYPE_BY_GW_ID: dict[int, FindingType] = {v: k for k, v in _FINDING_TYPE_GW_ID.items()}
