"""OpenVAS parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import ImportOptions, OpenVASScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = OpenVASScanner().parse(load("openvas_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Weak SSH Host Key"
    # CVSS base score 6.5 maps to Medium (per cvss_to_severity: 3.9 < x <= 6.9).
    assert findings[0].severity == Severity.MEDIUM
