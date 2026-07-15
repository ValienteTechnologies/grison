from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    INFO = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def from_str(cls, value: str) -> Severity:
        normalized = value.strip().lower()
        aliases = {
            "information": cls.INFO,
            "informational": cls.INFO,
            "info": cls.INFO,
            "none": cls.INFO,
            "low": cls.LOW,
            "medium": cls.MEDIUM,
            "moderate": cls.MEDIUM,
            "high": cls.HIGH,
            "critical": cls.CRITICAL,
        }
        if normalized not in aliases:
            raise ValueError(f"Unknown severity: {value!r}")
        return aliases[normalized]


_ORDER: list[Severity] = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


def cvss_to_severity(score: float) -> Severity:
    if score <= 0:
        return Severity.INFO
    if score <= 3.9:
        return Severity.LOW
    if score <= 6.9:
        return Severity.MEDIUM
    if score <= 8.9:
        return Severity.HIGH
    return Severity.CRITICAL


def parse_severity_filter(expr: str) -> set[Severity]:
    """Accept 'medium-critical' (range) or 'high,critical' (list)."""
    expr = expr.strip().lower()
    if not expr:
        return set(_ORDER)

    if "-" in expr and "," not in expr:
        parts = expr.split("-", 1)
        try:
            start = _ORDER.index(Severity.from_str(parts[0]))
            end = _ORDER.index(Severity.from_str(parts[1]))
        except ValueError as exc:
            raise ValueError(
                f"Invalid severity range {expr!r}. "
                f"Valid values: {', '.join(s.value for s in _ORDER)}"
            ) from exc
        lo, hi = min(start, end), max(start, end)
        return set(_ORDER[lo : hi + 1])

    result = set()
    for token in expr.split(","):
        result.add(Severity.from_str(token.strip()))
    return result
