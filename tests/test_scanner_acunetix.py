"""Acunetix parser tests (ported from gw-import)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.scanners import AcunetixScanner, ImportOptions
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def scanner() -> AcunetixScanner:
    return AcunetixScanner()


@pytest.fixture
def opts() -> ImportOptions:
    return ImportOptions()


def test_scanner_metadata() -> None:
    assert AcunetixScanner.name == "acunetix"
    assert AcunetixScanner.label == "Acunetix"


def test_parses_findings(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    assert len(findings) > 0


def test_aggregation_by_vuln_id(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # sqli-001 appears in both scans — should merge into one finding with 2 components
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    sqli = next(f for f in findings if f.plugin_id == "sqli-001")
    assert len(sqli.affected_components) == 2


def test_severity_text_high(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    sqli = next(f for f in findings if f.plugin_id == "sqli-001")
    assert sqli.severity == Severity.HIGH


def test_severity_numeric_medium(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # xss-001 has Severity=2 (numeric medium)
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    xss = next(f for f in findings if f.plugin_id == "xss-001")
    assert xss.severity == Severity.MEDIUM


def test_severity_numeric_critical(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # rce-001 has Severity=4 (numeric critical)
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    rce = next(f for f in findings if f.plugin_id == "rce-001")
    assert rce.severity == Severity.CRITICAL


def test_severity_numeric_info(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # info-001 has Severity=0 (numeric info)
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    info = next(f for f in findings if f.plugin_id == "info-001")
    assert info.severity == Severity.INFO


def test_inline_severity_numeric_critical(scanner: AcunetixScanner) -> None:
    xml = b"""<ScanGroup>
      <Scan>
        <StartURL>https://example.com</StartURL>
        <ReportItems>
          <ReportItem>
            <Name>Critical Issue</Name>
            <VulnID>crit-999</VulnID>
            <Severity>4</Severity>
          </ReportItem>
        </ReportItems>
      </Scan>
    </ScanGroup>"""
    findings = scanner.parse(xml, ImportOptions())
    assert findings[0].severity == Severity.CRITICAL


def test_cwe_numeric_normalised(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # sqli-001 has CWE=89 (no prefix)
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    sqli = next(f for f in findings if f.plugin_id == "sqli-001")
    assert sqli.cwe == "CWE-89"


def test_cwe_with_prefix_stripped(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    # xss-001 has CWE=CWE-79 (with prefix)
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    xss = next(f for f in findings if f.plugin_id == "xss-001")
    assert xss.cwe == "CWE-79"


def test_cve_tag_in_references(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    sqli = next(f for f in findings if f.plugin_id == "sqli-001")
    assert "CVE-2023-1234" in sqli.references


def test_severity_filter_critical_only(scanner: AcunetixScanner) -> None:
    findings = scanner.parse(
        load("acunetix_sample.xml"),
        ImportOptions(severity_filter={Severity.CRITICAL}),
    )
    assert all(f.severity == Severity.CRITICAL for f in findings)
    assert any(f.plugin_id == "rce-001" for f in findings)
    assert not any(f.plugin_id == "xss-001" for f in findings)


def test_plugin_exclude(scanner: AcunetixScanner) -> None:
    findings = scanner.parse(
        load("acunetix_sample.xml"),
        ImportOptions(exclude_plugins=["sqli-001"]),
    )
    assert not any(f.plugin_id == "sqli-001" for f in findings)


def test_plugin_include(scanner: AcunetixScanner) -> None:
    findings = scanner.parse(
        load("acunetix_sample.xml"),
        ImportOptions(include_plugins=["rce-001"]),
    )
    assert len(findings) == 1
    assert findings[0].plugin_id == "rce-001"


def test_findings_sorted_by_severity(scanner: AcunetixScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("acunetix_sample.xml"), opts)
    if len(findings) > 1:
        sev_order = list(Severity)
        indices = [sev_order.index(f.severity) for f in findings]
        assert indices == sorted(indices, reverse=True)


def test_empty_scan_group(scanner: AcunetixScanner) -> None:
    findings = scanner.parse(b"<ScanGroup></ScanGroup>", ImportOptions())
    assert findings == []


def test_inline_aggregation_across_scans(scanner: AcunetixScanner) -> None:
    xml = b"""<ScanGroup>
      <Scan>
        <StartURL>https://site1.example.com</StartURL>
        <ReportItems>
          <ReportItem>
            <Name>SQL Injection</Name>
            <VulnID>sqli-001</VulnID>
            <Severity>high</Severity>
            <AffectedItem>/login.php</AffectedItem>
          </ReportItem>
        </ReportItems>
      </Scan>
      <Scan>
        <StartURL>https://site2.example.com</StartURL>
        <ReportItems>
          <ReportItem>
            <Name>SQL Injection</Name>
            <VulnID>sqli-001</VulnID>
            <Severity>high</Severity>
            <AffectedItem>/search.php</AffectedItem>
          </ReportItem>
        </ReportItems>
      </Scan>
    </ScanGroup>"""
    findings = scanner.parse(xml, ImportOptions())
    assert len(findings) == 1
    assert len(findings[0].affected_components) == 2
