"""Nessus parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.markdown.mapping import ir_to_finding
from grison.model import FindingType
from grison.model.cvss import parse_cvss
from grison.scanners import ImportOptions, NessusScanner
from grison.scanners.ir import Severity
from grison.scanners.nessus import _cvss2_to_cvss3

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _report_item(inner: str) -> bytes:
    """Wrap a single ReportItem body in the minimal Nessus envelope."""
    return f"""<?xml version="1.0" ?>
<NessusClientData_v2>
  <Report name="example-scan">
    <ReportHost name="host.example.com">
      <ReportItem pluginID="1" pluginName="Test Plugin" severity="2">
        <risk_factor>Medium</risk_factor>
        {inner}
      </ReportItem>
    </ReportHost>
  </Report>
</NessusClientData_v2>
""".encode()


def test_parses_findings() -> None:
    findings = NessusScanner().parse(load("nessus_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Outdated TLS Version"
    assert findings[0].severity == Severity.MEDIUM
    assert findings[0].affected_components == ["host.example.com:443/tcp (https)"]
    assert findings[0].cwe == "CWE-79"


def test_cvss3_wins_over_v2_when_both_present() -> None:
    # Real .nessus exports commonly carry both fields; the v3 vector must win
    # verbatim, not get lossily reconverted from the v2 sibling.
    xml = _report_item(
        "<cvss_vector>CVSS2#AV:N/AC:L/Au:N/C:P/I:P/A:P</cvss_vector>"
        "<cvss3_vector>CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H</cvss3_vector>"
    )
    findings = NessusScanner().parse(xml, ImportOptions())
    assert findings[0].cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert parse_cvss(findings[0].cvss_vector).base_score == 9.8


def test_cvss3_bare_gets_prefixed_not_v2_converted() -> None:
    # cvss3_vector without its "CVSS:3.x/" prefix must still be treated as v3
    # (prefixed, not run through the v2 converter, which would zero its impact).
    xml = _report_item("<cvss3_vector>AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H</cvss3_vector>")
    findings = NessusScanner().parse(xml, ImportOptions())
    assert findings[0].cvss_vector == "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"

    result = ir_to_finding(findings[0], finding_type=FindingType.NETWORK)
    assert result.warnings == []
    assert result.finding.cvss is not None
    assert result.finding.cvss.score == 9.8


def test_cvss2_only_gets_converted() -> None:
    xml = _report_item("<cvss_vector>CVSS2#AV:N/AC:L/Au:N/C:P/I:P/A:P</cvss_vector>")
    findings = NessusScanner().parse(xml, ImportOptions())
    assert findings[0].cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L"

    result = ir_to_finding(findings[0], finding_type=FindingType.NETWORK)
    assert result.warnings == []
    assert result.finding.cvss is not None


def test_no_cvss_leaves_vector_empty() -> None:
    findings = NessusScanner().parse(_report_item(""), ImportOptions())
    assert findings[0].cvss_vector == ""


def test_cwe_uses_first_element() -> None:
    xml = _report_item("<cwe>79</cwe><cwe>89</cwe>")
    findings = NessusScanner().parse(xml, ImportOptions())
    assert findings[0].cwe == "CWE-79"


def test_cvss2_to_cvss3_preserves_av_with_hash_prefix() -> None:
    # Real Nessus vectors are prefixed "CVSS2#...": AV must not be dropped.
    v3 = _cvss2_to_cvss3("CVSS2#AV:L/AC:L/Au:N/C:P/I:P/A:P")
    assert "/AV:L/" in v3


def test_cvss2_to_cvss3_preserves_av_bare() -> None:
    v3 = _cvss2_to_cvss3("AV:A/AC:L/Au:N/C:P/I:P/A:P")
    assert "/AV:A/" in v3


def test_cvss2_to_cvss3_preserves_av_parenthesized() -> None:
    v3 = _cvss2_to_cvss3("(AV:N/AC:L/Au:N/C:P/I:P/A:P)")
    assert "/AV:N/" in v3


def test_cvss2_to_cvss3_full_conversion() -> None:
    v3 = _cvss2_to_cvss3("CVSS2#AV:N/AC:L/Au:N/C:P/I:P/A:P")
    assert v3 == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L"
