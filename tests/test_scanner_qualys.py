"""Qualys parser test (synthetic fixture, vuln-scan `<SCAN>` format)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import ImportOptions, QualysScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = QualysScanner().parse(load("qualys_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Outdated Apache Version"
    # severity="3" (unset on this VULN) is the _SEVERITY_MAP default -> Medium.
    assert findings[0].severity == Severity.MEDIUM
    assert findings[0].affected_components == ["192.0.2.30"]
