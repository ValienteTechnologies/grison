"""Nmap parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import ImportOptions, NmapScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    # nmap emits one Finding per host, listing all open ports as affected components.
    findings = NmapScanner().parse(load("nmap_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Open Ports – host.example.com"
    assert findings[0].severity == Severity.INFO
    assert len(findings[0].affected_components) == 2


def test_severity_filter_excludes_info_findings() -> None:
    # nmap findings are always INFO; --min-severity above INFO must suppress them.
    opts = ImportOptions(severity_filter={Severity.CRITICAL})
    findings = NmapScanner().parse(load("nmap_sample.xml"), opts)
    assert findings == []


def test_no_severity_filter_still_returns_findings() -> None:
    findings = NmapScanner().parse(load("nmap_sample.xml"), ImportOptions(severity_filter=None))
    assert len(findings) == 1
