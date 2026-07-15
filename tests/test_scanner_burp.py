"""Burp Suite parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import BurpScanner, ImportOptions
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = BurpScanner().parse(load("burp_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "SQL injection"
    assert findings[0].severity == Severity.HIGH
