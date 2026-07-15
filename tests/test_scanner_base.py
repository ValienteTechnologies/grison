"""Base-class helper tests: severity sort hoisted out of the individual parsers."""

from __future__ import annotations

from grison.scanners.base import Scanner
from grison.scanners.ir import Finding, Severity


def _f(sev: Severity) -> Finding:
    return Finding(title=f"{sev.value} finding", plugin_id=sev.value, severity=sev)


def test_sort_by_severity_most_severe_first() -> None:
    unsorted = [_f(Severity.LOW), _f(Severity.CRITICAL), _f(Severity.INFO), _f(Severity.HIGH)]
    ordered = Scanner.sort_by_severity(unsorted)
    assert [f.severity for f in ordered] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.LOW,
        Severity.INFO,
    ]


def test_sort_by_severity_is_stable_within_a_level() -> None:
    a, b = _f(Severity.MEDIUM), _f(Severity.MEDIUM)
    a.title, b.title = "first", "second"
    ordered = Scanner.sort_by_severity([a, b])
    assert [f.title for f in ordered] == ["first", "second"]
