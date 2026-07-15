"""Phase-4 tests: scanner IR → house Finding mapping rules."""

from __future__ import annotations

from grison.markdown.document import finding_to_markdown, markdown_to_finding
from grison.markdown.mapping import default_finding_type, ir_to_finding
from grison.model import FindingType, Severity
from grison.scanners.ir import Finding as IRFinding
from grison.scanners.ir import Severity as IRSeverity


def _ir(**over: object) -> IRFinding:
    base: dict[str, object] = {
        "title": "Reflected XSS",
        "plugin_id": "p1",
        "severity": IRSeverity.HIGH,
    }
    base.update(over)
    return IRFinding(**base)  # type: ignore[arg-type]


def test_mapping_core_rules() -> None:
    ir = _ir(
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
        cwe="CWE-79",
        description="<p>A <strong>reflected</strong> XSS in <code>q</code>.</p>",
        references='<ul><li><a href="https://ex.example/">ref</a></li></ul>',
        affected_components=["https://a.example/", "https://b.example/"],
        tags=["xss"],
    )
    res = ir_to_finding(ir, finding_type=FindingType.WEB)
    f = res.finding
    assert res.warnings == []
    assert f.severity is Severity.HIGH  # 1:1 from IR severity value
    assert f.finding_type is FindingType.WEB
    assert f.grison.tier == "instance"  # proto-instance
    assert f.grison.gw.id is None
    # affected_components -> affected_entities (one per line), NOT into replication_steps
    assert f.affected_entities == "https://a.example/\nhttps://b.example/"
    assert f.replication_steps == ""
    assert f.cwe == ["CWE-79"]
    assert "**reflected**" in f.description and "`q`" in f.description
    assert f.cvss is not None and f.cvss.vector.startswith("CVSS:3.1/")


def test_unknown_cwe_warns_and_drops() -> None:
    res = ir_to_finding(_ir(cwe="CWE-9999999"), finding_type=FindingType.NETWORK)
    assert res.finding.cwe == []
    assert any("unknown CWE" in w for w in res.warnings)


def test_invalid_cvss_warns_and_drops() -> None:
    res = ir_to_finding(
        _ir(cvss_vector="CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P"),
        finding_type=FindingType.NETWORK,
    )
    assert res.finding.cvss is None
    assert any("CVSS" in w for w in res.warnings)


def test_default_finding_type_by_scanner() -> None:
    assert default_finding_type("nessus") is FindingType.NETWORK
    assert default_finding_type("burp") is FindingType.WEB
    assert default_finding_type("acunetix") is FindingType.WEB
    assert default_finding_type("unknown-tool") is FindingType.NETWORK


def test_mapped_finding_serializes_and_roundtrips() -> None:
    res = ir_to_finding(
        _ir(description="<p>plain <em>note</em></p>", affected_components=["h1"]),
        finding_type=FindingType.WEB,
    )
    md = finding_to_markdown(res.finding)
    assert markdown_to_finding(md) == res.finding


def test_table_fallback_keeps_cells_separated() -> None:
    """The whitelist-degrade path must not run table cells together — an nmap port
    table previously collapsed to '22/tcpsshOpenSSH 8.9'."""
    from grison.markdown.mapping import _prose_to_md

    html = (
        "<table><thead><tr><th>Port</th><th>Service</th><th>Product</th></tr></thead>"
        "<tbody><tr><td>22/tcp</td><td>ssh</td><td>OpenSSH 8.9</td></tr>"
        "<tr><td>80/tcp</td><td>http</td><td>Apache 2.4</td></tr></tbody></table>"
    )
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert warnings  # still surfaced as degraded
    assert "Port | Service | Product" in out
    assert "22/tcp | ssh | OpenSSH 8.9" in out
    assert "80/tcp | http | Apache 2.4" in out
