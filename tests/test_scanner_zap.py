"""OWASP ZAP parser test (synthetic fixture, XML form)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import ImportOptions, ZapScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = ZapScanner().parse(load("zap_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Cross Site Scripting (Reflected)"
    # _RISKCODE_MAP maps riskcode "3" -> CRITICAL (not ZAP's own High/Medium/Low scale).
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].cwe == "CWE-79"


def test_missing_riskcode_falls_back_to_riskdesc_word() -> None:
    # No riskcode at all: must derive the same severity as an explicit riskcode "3",
    # not silently degrade to LOW via riskdesc[:1] ("H" is not a map key).
    alert = {
        "alertRef": "40012",
        "name": "Cross Site Scripting (Reflected)",
        "riskdesc": "High (Medium)",
    }
    aggregated: dict = {}
    ZapScanner()._aggregate(alert, aggregated, ImportOptions())
    assert aggregated["40012"]["severity"] == Severity.CRITICAL


def test_unknown_riskdesc_word_degrades_to_low() -> None:
    alert = {
        "alertRef": "99999",
        "name": "Mystery Alert",
        "riskdesc": "Bogus (Nonsense)",
    }
    aggregated: dict = {}
    ZapScanner()._aggregate(alert, aggregated, ImportOptions())
    assert aggregated["99999"]["severity"] == Severity.LOW
