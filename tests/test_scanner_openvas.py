"""OpenVAS parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.model.cvss import parse_cvss
from grison.scanners import ImportOptions, OpenVASScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _result(nvt_tags: str, description: str = "", severity: str = "5.0") -> bytes:
    """Wrap a single NVT `<tags>` payload in the minimal OpenVAS result envelope."""
    return f"""<?xml version="1.0" ?>
<report>
  <report>
    <results>
      <result id="r1">
        <name>Test</name>
        {description}
        <severity>{severity}</severity>
        <host>192.0.2.1<hostname>host.example.com</hostname></host>
        <port>80/tcp</port>
        <qod><value>80</value></qod>
        <nvt oid="1.2.3">
          <name>Test</name>
          <tags>{nvt_tags}</tags>
        </nvt>
      </result>
    </results>
  </report>
</report>
""".encode()


def test_parses_findings() -> None:
    findings = OpenVASScanner().parse(load("openvas_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Weak SSH Host Key"
    # CVSS base score 6.5 maps to Medium (per cvss_to_severity: 3.9 < x <= 6.9).
    assert findings[0].severity == Severity.MEDIUM
    # Already-prefixed v3 vector from cvss_base_vector must pass through untouched.
    assert findings[0].cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"


def test_bare_v2_cvss_base_vector_gets_converted() -> None:
    # v2 has no "CVSS:" prefix in its own spec — that bareness is the signal.
    xml = _result("summary=Test|cvss_base_vector=AV:N/AC:L/Au:N/C:P/I:P/A:P")
    findings = OpenVASScanner().parse(xml, ImportOptions())
    assert findings[0].cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L"
    assert parse_cvss(findings[0].cvss_vector).base_score > 0


def test_empty_summary_falls_back_to_description() -> None:
    xml = _result(
        "summary=|solution=Fix it",
        description="<description>Real description text</description>",
    )
    findings = OpenVASScanner().parse(xml, ImportOptions())
    assert findings[0].description == "Real description text"
