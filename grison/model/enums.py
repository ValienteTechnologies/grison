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


class EnumDriftError(RuntimeError):
    """Raised when Ghostwriter's live ``findingSeverity``/``findingType`` lookup tables
    disagree with the hardcoded gw_id maps above. Those ids are baked in at import
    time (never re-derived per instance), so a per-install re-seed or a Ghostwriter
    schema change that renumbers either table would otherwise silently mis-map every
    severity/finding-type on every record synced afterward — this is checked once, up
    front, so a real drift aborts loudly naming the row instead."""


def check_severity_drift(rows: list[dict]) -> None:
    """Verify live ``findingSeverity`` rows (``{id, severity, weight}``) against
    :data:`_SEVERITY_GW_ID`. Only the ``id -> severity name`` mapping is asserted —
    ``weight`` is fetched for context but not itself checked, since its exact live
    values aren't a documented contract, just internal GW sort order."""
    by_id = {row["id"]: row for row in rows}
    for sev, gw_id in _SEVERITY_GW_ID.items():
        row = by_id.get(gw_id)
        if row is None:
            raise EnumDriftError(
                f"findingSeverity id {gw_id} ({sev.value}) is missing on the live instance"
            )
        live = str(row.get("severity") or "").strip().lower()
        if live != sev.value:
            raise EnumDriftError(
                f"findingSeverity drift: live id {gw_id} is {row.get('severity')!r}, "
                f"grison expects {sev.value!r} — the severity gw_id map has drifted"
            )
    known_ids = set(_SEVERITY_GW_ID.values())
    for row in rows:
        if row["id"] not in known_ids:
            raise EnumDriftError(
                f"findingSeverity: unknown live row id {row['id']} "
                f"({row.get('severity')!r}) — not in grison's severity map"
            )


def check_finding_type_drift(rows: list[dict]) -> None:
    """Verify live ``findingType`` rows (``{id, findingType}``) against
    :data:`_FINDING_TYPE_GW_ID` — same shape as :func:`check_severity_drift`."""
    by_id = {row["id"]: row for row in rows}
    for ft, gw_id in _FINDING_TYPE_GW_ID.items():
        row = by_id.get(gw_id)
        if row is None:
            raise EnumDriftError(
                f"findingType id {gw_id} ({ft.value}) is missing on the live instance"
            )
        live = str(row.get("findingType") or "").strip().lower()
        if live != ft.value:
            raise EnumDriftError(
                f"findingType drift: live id {gw_id} is {row.get('finding_type')!r}, "
                f"grison expects {ft.value!r} — the finding-type gw_id map has drifted"
            )
    known_ids = set(_FINDING_TYPE_GW_ID.values())
    for row in rows:
        if row["id"] not in known_ids:
            raise EnumDriftError(
                f"findingType: unknown live row id {row['id']} "
                f"({row.get('finding_type')!r}) — not in grison's finding-type map"
            )
