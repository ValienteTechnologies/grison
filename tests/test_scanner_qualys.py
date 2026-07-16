"""Qualys parser tests (synthetic fixtures).

Covers both root formats: the vuln-scan `<SCAN>` format and the WAS
`<WAS_SCAN_REPORT>` format, whose QID glossary can carry a CVSS_V3 vector.
"""

from __future__ import annotations

from pathlib import Path

from grison.model.cvss import parse_cvss
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
    # The VM/vuln-scan fixture carries no CVSS element of any kind (no vector,
    # no numeric score) — _parse_vuln is intentionally left unchanged, so this
    # stays empty. See report for the open question on real VM XML shape.
    assert findings[0].cvss_vector == ""


def test_was_cvss_v3_vector_string_bare_gets_prefixed() -> None:
    findings = QualysScanner().parse(load("qualys_was_sample.xml"), ImportOptions())
    by_title = {f.title: f for f in findings}

    xss = by_title["Reflected Cross-Site Scripting"]
    assert xss.cvss_vector == "CVSS:3.0/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N"
    assert parse_cvss(xss.cvss_vector).base_score > 0


def test_was_cvss_v3_vector_string_already_prefixed_untouched() -> None:
    findings = QualysScanner().parse(load("qualys_was_sample.xml"), ImportOptions())
    by_title = {f.title: f for f in findings}

    sqli = by_title["SQL Injection"]
    assert sqli.cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert parse_cvss(sqli.cvss_vector).base_score == 9.8
