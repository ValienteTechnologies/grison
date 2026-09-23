"""OWASP ZAP parser test (synthetic fixture, XML form)."""

from __future__ import annotations

import json
from pathlib import Path

from grison.scanners import ImportOptions, ZapScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _alert_doc(alert: dict) -> bytes:
    """Wrap a single alert in ZAP's JSON export shape (site -> alerts -> [alert])."""
    return json.dumps({"site": [{"alerts": [alert]}]}).encode()


def test_parses_findings() -> None:
    findings = ZapScanner().parse(load("zap/zap_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Cross Site Scripting (Reflected)"
    # _RISKCODE_MAP maps riskcode "3" -> HIGH, matching ZAP's own High/Medium/Low scale
    # (ZAP has no Critical tier).
    assert findings[0].severity == Severity.HIGH
    assert findings[0].cwe == "CWE-79"
    # <confidence>2</confidence> -> ZAP's own 0..3 scale, medium.
    assert findings[0].tags == ["confidence:medium"]


def test_missing_riskcode_falls_back_to_riskdesc_word() -> None:
    # No riskcode at all: must derive the same severity as an explicit riskcode "3",
    # not silently degrade to INFO via riskdesc[:1] ("H" is not a map key).
    #
    # Was a direct call into ZapScanner._aggregate/_to_findings with a hand-built
    # `aggregated` dict; aggregation now lives in the shared Aggregator (base.py),
    # so this exercises the same behaviour through the public parse() API instead.
    alert = {
        "alertRef": "40012",
        "name": "Cross Site Scripting (Reflected)",
        "riskdesc": "High (Medium)",
    }
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].severity == Severity.HIGH


def test_riskdesc_word_moderate_resolves_via_severity_or_info() -> None:
    # "Moderate" is not a _RISKDESC_WORD_MAP key (only "medium" is), so this falls
    # through to severity_or_info, which resolves it through Severity.from_str's
    # "moderate" alias to MEDIUM — not the INFO fallback an unmapped word gets.
    alert = {
        "alertRef": "40018",
        "name": "SQL Injection",
        "riskdesc": "Moderate (Medium)",
    }
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].severity == Severity.MEDIUM


def test_unknown_riskdesc_word_degrades_to_info() -> None:
    alert = {
        "alertRef": "99999",
        "name": "Mystery Alert",
        "riskdesc": "Bogus (Nonsense)",
    }
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].severity == Severity.INFO


def test_cweid_sentinel_negative_one_yields_no_cwe() -> None:
    # ZAP emits cweid "-1" for unmapped alerts; that's not a real CWE ID.
    alert = {
        "alertRef": "12345",
        "name": "Unmapped Alert",
        "riskcode": "1",
        "cweid": "-1",
    }
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].cwe == ""


def test_merge_takes_max_severity() -> None:
    # Two instances of the same alert with different riskcodes should merge to the
    # higher severity, not freeze on whichever occurrence arrived first.
    doc = {
        "site": [
            {
                "alerts": [
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1"},
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "3"},
                ]
            }
        ]
    }
    findings = ZapScanner().parse(json.dumps(doc).encode(), ImportOptions())
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH


def test_confidence_tag_from_numeric_scale() -> None:
    alert = {"alertRef": "40012", "name": "XSS", "riskcode": "3", "confidence": "3"}
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].tags == ["confidence:high"]


def test_confidence_zero_is_false_positive_tag() -> None:
    alert = {"alertRef": "40013", "name": "XSS", "riskcode": "0", "confidence": "0"}
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert findings[0].tags == ["confidence:false-positive"]


def test_confidence_merge_keeps_highest() -> None:
    # Two alerts of the same alertRef, low confidence then high: the merged
    # finding keeps the highest confidence seen, not the first one.
    doc = {
        "site": [
            {
                "alerts": [
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1", "confidence": "1"},
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1", "confidence": "3"},
                ]
            }
        ]
    }
    findings = ZapScanner().parse(json.dumps(doc).encode(), ImportOptions())
    assert len(findings) == 1
    assert findings[0].tags == ["confidence:high"]

    # Reversed order: still keeps the highest, not "whichever came first".
    doc_reversed = {
        "site": [
            {
                "alerts": [
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1", "confidence": "3"},
                    {"alertRef": "1", "name": "Dupe Alert", "riskcode": "1", "confidence": "1"},
                ]
            }
        ]
    }
    findings_reversed = ZapScanner().parse(json.dumps(doc_reversed).encode(), ImportOptions())
    assert findings_reversed[0].tags == ["confidence:high"]


def test_instance_uri_is_escaped_in_replication_steps() -> None:
    """A ZAP alert instance's uri (and method/param) is vendor-supplied text that
    can itself be an XSS payload the scanner faithfully quotes back (real
    fixture: dojo-zap-results-first-scan.xml's `</stYle/</titLe/...` uri).
    Interpolated unescaped into replication_steps' <li> markup, it becomes real
    tags instead of quoted text — html_to_md then refuses the malformed HTML and
    the converter falls back, losing the payload from the rendered markdown.
    Every interpolated instance field must be html-escaped."""
    alert = {
        "alertRef": "40012",
        "name": "XSS",
        "riskcode": "3",
        "instances": [{"uri": "http://example.com/?q=<script>alert(1)</script>", "method": "GET"}],
    }
    findings = ZapScanner().parse(_alert_doc(alert), ImportOptions())
    assert len(findings) == 1
    assert "&lt;script&gt;" in findings[0].replication_steps
    assert "<script>" not in findings[0].replication_steps
