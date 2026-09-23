"""Phase-4 tests: scanner IR → format-v2 inbox finding mapping rules."""

from __future__ import annotations

from pathlib import Path

from grison.formats import finding as finding_fmt
from grison.markdown.mapping import default_finding_type, ir_to_finding
from grison.model import FindingType, Severity
from grison.scanners.ir import ScanFinding
from grison.scanners.ir import Severity as IRSeverity

_INBOX_PATH = Path("findings/inbox/x.md")


def _ir(**over: object) -> ScanFinding:
    base: dict[str, object] = {
        "title": "Reflected XSS",
        "plugin_id": "p1",
        "severity": IRSeverity.HIGH,
    }
    base.update(over)
    return ScanFinding(**base)  # type: ignore[arg-type]


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
    md = finding_fmt.dump(res.finding)
    assert finding_fmt.parse(md, path=_INBOX_PATH) == res.finding
    assert "grison:" not in md  # no machine field on grison parse output (engine step 3 item E)


def test_unsupported_tag_fallback_escapes_html_lookalikes() -> None:
    """FND-014 regression (burp/dojo-seven_findings.xml's XSS finding, see
    ``tests/scanners/test_contract.py``): an unsupported tag (``<b>``, real Burp
    output, not in the converter's GW whitelist) makes ``_prose_to_md`` fall back
    to ``_TagStripper``. The stripped text can itself contain a payload that
    looks like markup — here, an HTML-entity-escaped ``<script>`` tag decoded
    back to literal text by the HTML parser — and that text must come out
    backslash-escaped so it round-trips as plain text instead of being
    reparsed as raw inline HTML."""
    from grison.markdown.converter import md_to_html
    from grison.markdown.mapping import _prose_to_md

    html = (
        "<p>The payload <b>5d4ff&lt;script&gt;alert(1)&lt;/script&gt;18327</b> was submitted.</p>"
    )
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert any("unsupported HTML tag: <b>" in w for w in warnings)
    assert "\\<script>" in out and "\\</script>" in out  # escaped, not bare
    # Round-trips back to the original literal text, not real markup.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in md_to_html(out)


def test_unsupported_tag_fallback_does_not_double_unescape_entities() -> None:
    """A literal entity NAME in the source text (author prose describing HTML
    entities, e.g. ``&amp;lt;`` meaning the four characters ``&lt;``) must decode
    exactly once. ``HTMLParser`` (``convert_charrefs=True``, the default) already
    unescapes it once in ``handle_data``; running ``html.unescape`` on the result
    AGAIN would turn ``&amp;lt;`` into a real ``<`` character — silently
    fabricating markup that was never in the source. The fallback must decode
    once and then escape for markdown, not decode twice."""
    from grison.markdown.mapping import _prose_to_md

    html = "<p>Encode it as <b>&amp;lt;script&amp;gt;</b>, not the raw tag.</p>"
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out or "\\&lt;script\\&gt;" in out


def test_nmap_table_converts_to_real_markdown_table() -> None:
    """The converter now supports GFM tables (grammar widened 2026-09-21), so an
    nmap port table (real shape: ``nmap.py``'s own ``<thead>``/``<tbody>``, no
    ``<p>`` in cells) converts cleanly via ``html_to_md`` — no more falling back
    to the whitelist-degrade ``_TagStripper`` path (which used to run cells
    together: '22/tcpsshOpenSSH 8.9')."""
    from grison.markdown.mapping import _prose_to_md

    html = (
        "<table><thead><tr><th>Port</th><th>Service</th><th>Product</th></tr></thead>"
        "<tbody><tr><td>22/tcp</td><td>ssh</td><td>OpenSSH 8.9</td></tr>"
        "<tr><td>80/tcp</td><td>http</td><td>Apache 2.4</td></tr></tbody></table>"
    )
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert warnings == []  # clean conversion, no degrade
    assert out == (
        "| Port | Service | Product |\n"
        "| --- | --- | --- |\n"
        "| 22/tcp | ssh | OpenSSH 8.9 |\n"
        "| 80/tcp | http | Apache 2.4 |"
    )
