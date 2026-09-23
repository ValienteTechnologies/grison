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
    ``tests/scanners/test_contract.py``): an unsupported tag (``<u>`` here — real
    Burp output uses ``<b>``/``<i>``, but those are now rewritten to their GW
    equivalents by ``_normalize_scanner_html`` before this fallback ever runs;
    see ``test_presentational_tags_are_rewritten_not_stripped`` below — so a tag
    with no whitelisted alias is needed to still exercise the fallback) makes
    ``_prose_to_md`` fall back to ``_TagStripper``. The stripped text can itself
    contain a payload that looks like markup — here, an HTML-entity-escaped
    ``<script>`` tag decoded back to literal text by the HTML parser — and that
    text must come out backslash-escaped so it round-trips as plain text instead
    of being reparsed as raw inline HTML."""
    from grison.markdown.converter import md_to_html
    from grison.markdown.mapping import _prose_to_md

    html = (
        "<p>The payload <u>5d4ff&lt;script&gt;alert(1)&lt;/script&gt;18327</u> was submitted.</p>"
    )
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert any("unsupported HTML tag: <u>" in w for w in warnings)
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

    html = "<p>Encode it as <u>&amp;lt;script&amp;gt;</u>, not the raw tag.</p>"
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out or "\\&lt;script\\&gt;" in out


def test_presentational_tags_are_rewritten_not_stripped() -> None:
    """``<b>``/``<i>`` (real Burp/ZAP output, not in the converter's GW whitelist
    on their own) are rewritten to ``<strong>``/``<em>`` before conversion, so
    they come out as real markdown bold/italic instead of degrading through the
    lenient ``_TagStripper`` fallback."""
    from grison.markdown.mapping import _prose_to_md

    html = "<p>The <b>X-Frame-Options</b> header and <i>every</i> variable.</p>"
    warnings: list[str] = []
    out = _prose_to_md(html, "description", warnings)
    assert warnings == []
    assert "**X-Frame-Options**" in out
    assert "*every*" in out


def test_tag_alias_rewriter_leaves_script_body_raw() -> None:
    """``handle_data`` must not ``html.escape`` the content of ``<script>``/
    ``<style>`` elements: ``HTMLParser`` hands those bodies over as raw,
    un-charref-converted CDATA (that's what ``CDATA_CONTENT_ELEMENTS`` means),
    and escaping it like ordinary text would corrupt it (``a<b`` turning into
    the literal text ``a&lt;b``)."""
    from grison.markdown.mapping import _normalize_scanner_html

    raw = "<script>if (a<b) { x(); }</script><style>.c{color:a<b}</style>"
    out = _normalize_scanner_html(raw)
    assert "a<b" in out
    assert "&lt;" not in out


def test_tag_alias_rewriter_passes_boolean_attribute_through() -> None:
    """A boolean attribute (no value, e.g. ``disabled`` in ``<input disabled>``)
    must pass through as a bare name — dropping it silently loses it, and
    fabricating an empty value (``disabled=""``) changes what was in the
    source."""
    from grison.markdown.mapping import _normalize_scanner_html

    assert _normalize_scanner_html("<input disabled>") == "<input disabled>"


# -- _StructureNormalizer (real-world scanner HTML structural cleanup) --------
#
# Adversarial cases for each of the three rewrite rules: an unclosed <p> auto-
# closed at a block-level trigger or end-of-input, <div> unwrapping, and the
# <dl>/<dt>/<dd> -> <ul><li><strong>...</strong> ...</li></ul> rewrite. Each
# case is checked two ways: the normalizer's own text output (the exact shape
# rewritten), and that the full _prose_to_md conversion succeeds with zero
# fallback warnings — proving the rewrite actually unblocks real markdown
# conversion, not just that it produces SOME string.


def test_unclosed_p_nested_inside_li_is_closed_by_the_next_p() -> None:
    # Two sibling <p>s inside one <li>, only the second one explicitly closed —
    # real Qualys-style prose never closes a <p> at all. The first must be
    # auto-closed the moment the second <p> starts, or the unclosed <p> ends up
    # nested one level too deep and the eventual </li> mismatches it instead.
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<ul><li><p>foo<p>bar</p></li></ul>"
    assert _normalize_scanner_html(raw) == "<ul><li><p>foo</p><p>bar</p></li></ul>"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "- foo\n\n  bar"


def test_unclosed_p_inside_blockquote_is_closed_by_the_next_p() -> None:
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<blockquote><p>text<p>more</p></blockquote>"
    assert _normalize_scanner_html(raw) == "<blockquote><p>text</p><p>more</p></blockquote>"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "> text\n>\n> more"


def test_uppercase_p_is_normalized_and_auto_closed() -> None:
    # Real Qualys exports spell the tag "<P>" — HTMLParser itself lowercases
    # tag names, but the auto-close logic must still see it as "p".
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<P>Hello<P>World"
    assert _normalize_scanner_html(raw) == "<p>Hello</p><p>World</p>"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "Hello\n\nWorld"


def test_p_immediately_followed_by_ul_is_closed_before_the_list_opens() -> None:
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<P>Intro<UL><LI>one</LI><LI>two</LI></UL>"
    assert _normalize_scanner_html(raw) == "<p>Intro</p><ul><li>one</li><li>two</li></ul>"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "Intro\n\n- one\n- two"


def test_div_with_attributes_is_unwrapped() -> None:
    # Real Acunetix output: <div class="bb-coolbox"><span ...>...</span></div>.
    # The wrapper (tag AND attributes) is dropped; only its children survive.
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = '<div class="bb-coolbox" id="x">Manual confirmation required</div>'
    assert _normalize_scanner_html(raw) == "Manual confirmation required"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "Manual confirmation required"


def test_dl_with_several_dt_dd_pairs_becomes_a_list() -> None:
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<dl><dt>Term1</dt><dd>Def1</dd><dt>Term2</dt><dd>Def2</dd></dl>"
    assert _normalize_scanner_html(raw) == (
        "<ul><li><strong>Term1</strong> Def1</li><li><strong>Term2</strong> Def2</li></ul>"
    )

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "- **Term1** Def1\n- **Term2** Def2"


def test_dl_missing_dd_still_closes_its_li() -> None:
    # A <dt> with no following <dd> at all must not leave its <li> unclosed —
    # closed by </dl> (end-of-input for a <dl> works the same way).
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<dl><dt>OnlyTerm</dt></dl>"
    assert _normalize_scanner_html(raw) == "<ul><li><strong>OnlyTerm</strong> </li></ul>"

    warnings: list[str] = []
    out = _prose_to_md(raw, "description", warnings)
    assert warnings == []
    assert out == "- **OnlyTerm**"


def test_unknown_tag_is_left_alone_and_still_refused() -> None:
    # <limit> (real OpenVAS content, quoting an Apache <Limit> directive) has
    # no rewrite rule — it must pass through untouched, so the converter still
    # refuses it exactly as before this normalization pass existed.
    from grison.markdown.mapping import _normalize_scanner_html, _prose_to_md

    raw = "<p>See the <limit>Limit</limit> directive.</p>"
    assert _normalize_scanner_html(raw) == "<p>See the <limit>Limit</limit> directive.</p>"

    warnings: list[str] = []
    _prose_to_md(raw, "description", warnings)
    assert len(warnings) == 1
    assert "unsupported HTML tag: <limit>" in warnings[0]


def test_mismatched_closing_tag_is_left_alone_not_repaired() -> None:
    # A <b> spanning across an unclosed <p> boundary (real Qualys shape) is a
    # genuine mismatch between two DIFFERENT tags — this pass only ever closes
    # <p>, and only when <p> is the innermost open element; it never guesses at
    # a repair here, so the same mismatch the converter always raised is still
    # what it raises.
    from grison.markdown.mapping import _prose_to_md

    raw = "<p><b>bold text<p>more</b></p>"
    warnings: list[str] = []
    _prose_to_md(raw, "description", warnings)
    assert len(warnings) == 1
    assert "mismatched closing tag: </strong>" in warnings[0]


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


def test_burp_zap_corpus_hits_zero_fallback_on_every_prose_field() -> None:
    """Corpus check for the presentational-tag rewrite (2026-09) and the ZAP
    instance-field escaping fix (see ``tests/scanners/test_scanner_zap.py``'s
    ``test_instance_uri_is_escaped_in_replication_steps``): before either fix,
    running every real burp/zap fixture's prose fields through ``_prose_to_md``
    hit the ``_TagStripper`` fallback — the presentational-tag cases (12 of 138
    description/mitigation fields, all ``<b>``/``<i>``) plus, for
    ``replication_steps``, a ZAP alert instance whose uri carried unescaped
    payload text that broke the generated ``<li>`` markup (real fixture:
    dojo-zap-results-first-scan.xml's `</stYle/</titLe/...` uri). Covering all
    four prose fields (not just description/mitigation) is what would have
    caught that replication_steps regression."""
    from grison.markdown.mapping import _prose_to_md
    from grison.scanners import BurpScanner, ImportOptions, ZapScanner
    from grison.scanners.detect import normalise_input

    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "scanners"
    opts = ImportOptions()
    total_fields = 0
    for scanner_cls, subdir in ((BurpScanner, "burp"), (ZapScanner, "zap")):
        for path in sorted((fixtures_dir / subdir).iterdir()):
            if not path.is_file():
                continue
            try:
                findings = scanner_cls().parse(normalise_input(path.read_bytes()), opts)
            except Exception:  # noqa: BLE001 — a handful of fixtures don't parse; not this test's concern
                continue
            for finding in findings:
                for field in ("description", "impact", "mitigation", "replication_steps"):
                    value = getattr(finding, field)
                    if not value.strip():
                        continue
                    total_fields += 1
                    warnings: list[str] = []
                    _prose_to_md(value, field, warnings)
                    assert not any("HTML outside GW whitelist" in w for w in warnings), (
                        f"{subdir}/{path.name} {field}: unexpected fallback hit: {warnings}"
                    )
    assert total_fields > 0  # the corpus walk itself actually ran
