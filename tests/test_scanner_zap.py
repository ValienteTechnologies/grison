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
    # _RISKCODE_MAP maps riskcode "3" -> HIGH, matching ZAP's own High/Medium/Low scale
    # (ZAP has no Critical tier).
    assert findings[0].severity == Severity.HIGH
    assert findings[0].cwe == "CWE-79"


def test_missing_riskcode_falls_back_to_riskdesc_word() -> None:
    # No riskcode at all: must derive the same severity as an explicit riskcode "3",
    # not silently degrade to INFO via riskdesc[:1] ("H" is not a map key).
    alert = {
        "alertRef": "40012",
        "name": "Cross Site Scripting (Reflected)",
        "riskdesc": "High (Medium)",
    }
    aggregated: dict = {}
    ZapScanner()._aggregate(alert, aggregated, ImportOptions())
    assert aggregated["40012"]["severity"] == Severity.HIGH


def test_unknown_riskdesc_word_degrades_to_info() -> None:
    alert = {
        "alertRef": "99999",
        "name": "Mystery Alert",
        "riskdesc": "Bogus (Nonsense)",
    }
    aggregated: dict = {}
    ZapScanner()._aggregate(alert, aggregated, ImportOptions())
    assert aggregated["99999"]["severity"] == Severity.INFO


def test_cweid_sentinel_negative_one_yields_no_cwe() -> None:
    # ZAP emits cweid "-1" for unmapped alerts; that's not a real CWE ID.
    alert = {
        "alertRef": "12345",
        "name": "Unmapped Alert",
        "riskcode": "1",
        "cweid": "-1",
    }
    aggregated: dict = {}
    ZapScanner()._aggregate(alert, aggregated, ImportOptions())
    findings = ZapScanner()._to_findings(aggregated)
    assert findings[0].cwe == ""


def test_merge_takes_max_severity() -> None:
    # Two instances of the same alert with different riskcodes should merge to the
    # higher severity, not freeze on whichever occurrence arrived first.
    scanner = ZapScanner()
    aggregated: dict = {}
    scanner._aggregate(
        {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1"}, aggregated, ImportOptions()
    )
    scanner._aggregate(
        {"alertRef": "1", "name": "Dupe Alert", "riskcode": "3"}, aggregated, ImportOptions()
    )
    assert aggregated["1"]["severity"] == Severity.HIGH
