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


def test_reference_anchor_text_preserved() -> None:
    # The references field carries HTML-escaped <a> tags; the rebuild must keep the
    # original anchor text instead of collapsing it down to the bare URL.
    findings = BurpScanner().parse(load("burp_sample.xml"), ImportOptions())
    assert (
        '<a href="https://portswigger.net/kb/issues/00100200_sql-injection">'
        "SQL injection</a>" in findings[0].references
    )


def test_merge_takes_max_severity() -> None:
    # Two issues of the same type_id with different severities should merge to the
    # higher severity, not freeze on whichever occurrence arrived first.
    xml = b"""<issues>
      <issue>
        <serialNumber>1</serialNumber>
        <type>1048832</type>
        <name>SQL injection</name>
        <host ip="192.0.2.10">https://example.com</host>
        <path>/login</path>
        <location>/login</location>
        <severity>Low</severity>
      </issue>
      <issue>
        <serialNumber>2</serialNumber>
        <type>1048832</type>
        <name>SQL injection</name>
        <host ip="192.0.2.11">https://example.com</host>
        <path>/admin</path>
        <location>/admin</location>
        <severity>High</severity>
      </issue>
    </issues>"""
    findings = BurpScanner().parse(xml, ImportOptions())
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
