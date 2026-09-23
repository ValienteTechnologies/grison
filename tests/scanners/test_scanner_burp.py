"""Burp Suite parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import BurpScanner, ImportOptions
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = BurpScanner().parse(load("burp/burp_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "SQL injection"
    assert findings[0].severity == Severity.HIGH


def test_reference_anchor_text_preserved() -> None:
    # The references field carries HTML-escaped <a> tags; the rebuild must keep the
    # original anchor text instead of collapsing it down to the bare URL.
    findings = BurpScanner().parse(load("burp/burp_sample.xml"), ImportOptions())
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


def test_confidence_tag() -> None:
    # burp_sample.xml's single issue has <confidence>Certain</confidence>.
    findings = BurpScanner().parse(load("burp/burp_sample.xml"), ImportOptions())
    assert findings[0].tags == ["confidence:certain"]


def _issue(serial: str, severity: str, confidence: str) -> str:
    return f"""
      <issue>
        <serialNumber>{serial}</serialNumber>
        <type>1048832</type>
        <name>SQL injection</name>
        <host ip="192.0.2.10">https://example.com</host>
        <path>/login</path>
        <location>/login</location>
        <severity>{severity}</severity>
        <confidence>{confidence}</confidence>
      </issue>"""


def test_confidence_merge_keeps_highest_first_then_second() -> None:
    # First occurrence Tentative, second Certain: merged finding keeps Certain
    # (the highest/most-certain), not the first one seen.
    xml = (
        "<issues>" + _issue("1", "Low", "Tentative") + _issue("2", "High", "Certain") + "</issues>"
    ).encode()
    findings = BurpScanner().parse(xml, ImportOptions())
    assert len(findings) == 1
    assert findings[0].tags == ["confidence:certain"]


def test_confidence_merge_keeps_highest_second_then_first() -> None:
    # Reversed order: first occurrence Certain, second Tentative — still Certain,
    # confirming the merge isn't "whichever occurrence sets it first" either.
    xml = (
        "<issues>" + _issue("1", "Low", "Certain") + _issue("2", "High", "Tentative") + "</issues>"
    ).encode()
    findings = BurpScanner().parse(xml, ImportOptions())
    assert len(findings) == 1
    assert findings[0].tags == ["confidence:certain"]
