"""Salvage-patch test: Acunetix CVSS extraction from <CVSS3><Descriptor>.

gw-import's Acunetix parser dropped CVSS entirely. Real exports carry a clean
`CVSS:3.1/…` string in <CVSS3><Descriptor> — this asserts grison reads it.
"""

from __future__ import annotations

from grison.scanners import AcunetixScanner, ImportOptions

_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ScanGroup>
  <Scan>
    <StartURL>https://example.com/</StartURL>
    <ReportItems>
      <ReportItem id="1">
        <Name>Example finding</Name>
        <Severity>2</Severity>
        <CVSS><Descriptor>AV:N/AC:L/Au:N/C:P/I:P/A:P</Descriptor></CVSS>
        <CVSS3><Descriptor><![CDATA[CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:N/I:N/A:N]]></Descriptor></CVSS3>
        <CVSS4><Descriptor>CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:P/VC:N/VI:N/VA:N/SC:N/SI:N/SA:N</Descriptor></CVSS4>
        <AffectedItem>/login</AffectedItem>
      </ReportItem>
    </ReportItems>
  </Scan>
</ScanGroup>
"""


def test_acunetix_reads_cvss3_descriptor() -> None:
    findings = AcunetixScanner().parse(_XML, ImportOptions())
    assert len(findings) == 1
    # The clean 3.1 descriptor is read; the v2/v4 siblings are ignored.
    assert findings[0].cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:N/I:N/A:N"


def test_acunetix_no_cvss3_leaves_vector_empty() -> None:
    xml = b"""<ScanGroup><Scan><StartURL>https://example.com/</StartURL>
      <ReportItems><ReportItem id="1"><Name>No cvss</Name><Severity>1</Severity>
      </ReportItem></ReportItems></Scan></ScanGroup>"""
    findings = AcunetixScanner().parse(xml, ImportOptions())
    assert findings[0].cvss_vector == ""
