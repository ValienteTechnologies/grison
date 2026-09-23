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


# Canonical least-to-most-severe ordering — the one source of truth for severity
# order. Nothing else (base.py included) derives an order separately.
SEVERITY_ORDER: list[Severity] = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


def max_severity(a: Severity, b: Severity) -> Severity:
    """Return the more severe of two severities (for merging occurrences of the
    same finding). Ties keep ``b``'s side of the comparison, i.e. either argument
    when equal."""
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b


def severity_or(raw: str, default: Severity) -> Severity:
    """Parse a free-text severity via :meth:`Severity.from_str`, falling back to
    ``default`` for anything unrecognized (blank, garbage, a bare numeric code a
    scanner-specific table should have already handled, ...)."""
    try:
        return Severity.from_str(raw)
    except ValueError:
        return default


def severity_or_info(raw: str) -> Severity:
    """``severity_or`` with the common fallback of :attr:`Severity.INFO`."""
    return severity_or(raw, Severity.INFO)


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
        return set(SEVERITY_ORDER)

    if "-" in expr and "," not in expr:
        parts = expr.split("-", 1)
        try:
            start = SEVERITY_ORDER.index(Severity.from_str(parts[0]))
            end = SEVERITY_ORDER.index(Severity.from_str(parts[1]))
        except ValueError as exc:
            raise ValueError(
                f"Invalid severity range {expr!r}. "
                f"Valid values: {', '.join(s.value for s in SEVERITY_ORDER)}"
            ) from exc
        lo, hi = min(start, end), max(start, end)
        return set(SEVERITY_ORDER[lo : hi + 1])

    result = set()
    for token in expr.split(","):
        result.add(Severity.from_str(token.strip()))
    return result
