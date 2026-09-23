"""Qualys parser tests (synthetic fixtures).

Covers both root formats: the vuln-scan `<SCAN>` format and the WAS
`<WAS_SCAN_REPORT>` format, whose QID glossary can carry a CVSS_V3 vector.
"""

from __future__ import annotations

from pathlib import Path

from grison.model.cvss import parse_cvss
from grison.scanners import ImportOptions, QualysScanner
from grison.scanners.ir import Severity
from grison.scanners.qualys import _parse_severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = QualysScanner().parse(load("qualys/qualys_sample.xml"), ImportOptions())
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
    findings = QualysScanner().parse(load("qualys/qualys_was_sample.xml"), ImportOptions())
    by_title = {f.title: f for f in findings}

    xss = by_title["Reflected Cross-Site Scripting"]
    assert xss.cvss_vector == "CVSS:3.0/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N"
    assert parse_cvss(xss.cvss_vector).base_score > 0


def test_unrecognised_severity_code_yields_info() -> None:
    # The project-wide rule (July 2026 parser audit): an unrecognised severity
    # code falls back to INFO everywhere, same as every other parser's
    # severity_or_info — not Qualys's own historical Medium default.
    assert _parse_severity("9") == Severity.INFO
    assert _parse_severity("") == Severity.INFO


def test_was_cvss_v3_vector_string_already_prefixed_untouched() -> None:
    findings = QualysScanner().parse(load("qualys/qualys_was_sample.xml"), ImportOptions())
    by_title = {f.title: f for f in findings}

    sqli = by_title["SQL Injection"]
    assert sqli.cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert parse_cvss(sqli.cvss_vector).base_score == 9.8


# --- ASSET_DATA_REPORT (VM export) -------------------------------------------

_VM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ASSET_DATA_REPORT>
  <HOST_LIST>
    <HOST>
      <IP>192.0.2.10</IP>
      <DNS>host-a.example.com</DNS>
      <VULN_INFO_LIST>
        <VULN_INFO>
          <QID id="qid_100001">100001</QID>
          <PORT>443</PORT>
        </VULN_INFO>
        <VULN_INFO>
          <QID id="qid_100002">100002</QID>
        </VULN_INFO>
      </VULN_INFO_LIST>
    </HOST>
    <HOST>
      <IP>192.0.2.11</IP>
      <VULN_INFO_LIST>
        <VULN_INFO>
          <QID id="qid_100001">100001</QID>
          <PORT>443</PORT>
        </VULN_INFO>
      </VULN_INFO_LIST>
    </HOST>
  </HOST_LIST>
  <GLOSSARY>
    <VULN_DETAILS_LIST>
      <VULN_DETAILS id="qid_100001">
        <QID id="qid_100001">100001</QID>
        <TITLE><![CDATA[Weak TLS Cipher Suite]]></TITLE>
        <SEVERITY>4</SEVERITY>
        <THREAT><![CDATA[The server accepts a weak cipher suite.]]></THREAT>
        <IMPACT><![CDATA[Traffic may be decrypted by an attacker.]]></IMPACT>
        <SOLUTION><![CDATA[Disable weak cipher suites.]]></SOLUTION>
        <CVSS3_SCORE>
          <CVSS3_BASE>7.4 (AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N)</CVSS3_BASE>
        </CVSS3_SCORE>
        <CVE_ID_LIST>
          <CVE_ID>
            <ID><![CDATA[CVE-2099-0001]]></ID>
            <URL><![CDATA[http://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2099-0001]]></URL>
          </CVE_ID>
        </CVE_ID_LIST>
        <VENDOR_REFERENCE_LIST>
          <VENDOR_REFERENCE>
            <ID><![CDATA[VENDOR-BULLETIN-1]]></ID>
            <URL><![CDATA[https://vendor.example.com/bulletin-1]]></URL>
          </VENDOR_REFERENCE>
        </VENDOR_REFERENCE_LIST>
      </VULN_DETAILS>
      <VULN_DETAILS id="qid_100002">
        <QID id="qid_100002">100002</QID>
        <TITLE><![CDATA[DNS Host Name]]></TITLE>
        <SEVERITY>1</SEVERITY>
        <THREAT><![CDATA[Informational: DNS host name.]]></THREAT>
        <IMPACT><![CDATA[N/A]]></IMPACT>
        <SOLUTION><![CDATA[N/A]]></SOLUTION>
      </VULN_DETAILS>
    </VULN_DETAILS_LIST>
  </GLOSSARY>
</ASSET_DATA_REPORT>
"""


def test_asset_data_report_root_detected_and_parsed() -> None:
    from grison.scanners import detect_bytes

    assert detect_bytes(_VM_XML) == "qualys"
    findings = QualysScanner().parse(_VM_XML, ImportOptions())
    by_title = {f.title: f for f in findings}
    assert set(by_title) == {"Weak TLS Cipher Suite", "DNS Host Name"}


def test_asset_data_report_aggregates_across_hosts_by_qid() -> None:
    # QID 100001 appears on both hosts — one finding, two affected components,
    # same shape as the Aggregator merges every other scanner's occurrences.
    findings = QualysScanner().parse(_VM_XML, ImportOptions())
    weak_tls = next(f for f in findings if f.title == "Weak TLS Cipher Suite")
    assert set(weak_tls.affected_components) == {
        "host-a.example.com:443",
        "192.0.2.11:443",
    }


def test_asset_data_report_maps_glossary_fields() -> None:
    findings = QualysScanner().parse(_VM_XML, ImportOptions())
    weak_tls = next(f for f in findings if f.title == "Weak TLS Cipher Suite")
    assert weak_tls.severity == Severity.HIGH  # SEVERITY 4
    assert weak_tls.description == "The server accepts a weak cipher suite."
    assert weak_tls.impact == "Traffic may be decrypted by an attacker."
    assert weak_tls.mitigation == "Disable weak cipher suites."
    # CVSS3_SCORE/CVSS3_BASE's parenthesised vector, prefixed like Nessus's and
    # WAS's bare cvss3_vector handling.
    assert weak_tls.cvss_vector == "CVSS:3.0/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N"
    assert "CVE-2099-0001" in weak_tls.references
    assert "VENDOR-BULLETIN-1" in weak_tls.references


def test_asset_data_report_host_without_dns_uses_ip() -> None:
    findings = QualysScanner().parse(_VM_XML, ImportOptions())
    weak_tls = next(f for f in findings if f.title == "Weak TLS Cipher Suite")
    assert "192.0.2.11:443" in weak_tls.affected_components  # no DNS on this host


def test_asset_data_report_no_hosts_yields_no_findings() -> None:
    empty = b"<ASSET_DATA_REPORT><HEADER/></ASSET_DATA_REPORT>"
    findings = QualysScanner().parse(empty, ImportOptions())
    assert findings == []


# --- WAS INFORMATION_GATHERED_LIST -------------------------------------------

_WAS_WITH_INFO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WAS_SCAN_REPORT>
  <RESULTS>
    <VULNERABILITY_LIST>
      <VULNERABILITY>
        <QID>200001</QID>
        <URL><![CDATA[https://app.example.com/vuln]]></URL>
      </VULNERABILITY>
    </VULNERABILITY_LIST>
    <INFORMATION_GATHERED_LIST>
      <INFORMATION_GATHERED>
        <QID>200002</QID>
      </INFORMATION_GATHERED>
      <INFORMATION_GATHERED>
        <QID>200001</QID>
      </INFORMATION_GATHERED>
    </INFORMATION_GATHERED_LIST>
  </RESULTS>
  <GLOSSARY>
    <QID_LIST>
      <QID>
        <QID>200001</QID>
        <TITLE><![CDATA[A Vulnerability]]></TITLE>
        <SEVERITY>4</SEVERITY>
      </QID>
      <QID>
        <QID>200002</QID>
        <TITLE><![CDATA[Some Info Gathered]]></TITLE>
        <SEVERITY>2</SEVERITY>
      </QID>
    </QID_LIST>
  </GLOSSARY>
</WAS_SCAN_REPORT>
"""


def test_was_information_gathered_items_become_findings() -> None:
    findings = QualysScanner().parse(_WAS_WITH_INFO_XML, ImportOptions())
    titles = {f.title for f in findings}
    assert "Some Info Gathered" in titles


def test_was_qid_in_both_vuln_and_info_gathered_lists_is_one_finding() -> None:
    # QID 200001 is in both lists (a synthetic edge case; not seen in the real
    # corpus) — the shared Aggregator dedupes by QID same as it does within a
    # single list, so this is still one finding, not two.
    findings = QualysScanner().parse(_WAS_WITH_INFO_XML, ImportOptions())
    matching = [f for f in findings if f.plugin_id == "200001"]
    assert len(matching) == 1
    assert matching[0].title == "A Vulnerability"


# --- WAS INFORMATION_GATHERED GROUP-based severity override -----------------

_WAS_IG_GROUP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WAS_SCAN_REPORT>
  <RESULTS>
    <INFORMATION_GATHERED_LIST>
      <INFORMATION_GATHERED>
        <QID>300001</QID>
      </INFORMATION_GATHERED>
      <INFORMATION_GATHERED>
        <QID>300002</QID>
      </INFORMATION_GATHERED>
    </INFORMATION_GATHERED_LIST>
  </RESULTS>
  <GLOSSARY>
    <QID_LIST>
      <QID>
        <QID>300001</QID>
        <TITLE><![CDATA[Connection Error Occurred During Web Application Scan]]></TITLE>
        <SEVERITY>2</SEVERITY>
        <GROUP>IG-DIAG</GROUP>
      </QID>
      <QID>
        <QID>300002</QID>
        <TITLE><![CDATA[Missing header: X-Content-Type-Options]]></TITLE>
        <SEVERITY>2</SEVERITY>
        <GROUP>IG-WEAK</GROUP>
      </QID>
    </QID_LIST>
  </GLOSSARY>
</WAS_SCAN_REPORT>
"""


def test_was_ig_diag_group_forces_info_severity() -> None:
    # Scan-diagnostic "information gathered" items (GROUP IG-DIAG, e.g. QID
    # 150018/45017 in the real corpus) are scan mechanics, not security
    # observations, even though the glossary carries a numeric SEVERITY.
    findings = QualysScanner().parse(_WAS_IG_GROUP_XML, ImportOptions())
    by_title = {f.title: f for f in findings}
    assert by_title["Connection Error Occurred During Web Application Scan"].severity == (
        Severity.INFO
    )


def test_was_ig_weak_group_keeps_numeric_severity() -> None:
    # IG-WEAK items (missing headers, cookies, HSTS...) are reportable
    # weaknesses, so they keep the glossary's numeric severity map.
    findings = QualysScanner().parse(_WAS_IG_GROUP_XML, ImportOptions())
    by_title = {f.title: f for f in findings}
    assert by_title["Missing header: X-Content-Type-Options"].severity == Severity.LOW


def test_was_bare_diag_group_spelling_also_forces_info() -> None:
    # Older WAS exports spell the group without the "IG-" prefix (bare "DIAG",
    # as in the reptor fixture); it means the same thing.
    xml = _WAS_IG_GROUP_XML.replace(b"<GROUP>IG-DIAG</GROUP>", b"<GROUP>DIAG</GROUP>")
    findings = QualysScanner().parse(xml, ImportOptions())
    by_title = {f.title: f for f in findings}
    assert by_title["Connection Error Occurred During Web Application Scan"].severity == (
        Severity.INFO
    )
