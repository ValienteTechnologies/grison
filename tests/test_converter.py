"""Tests for the bespoke HTML<->markdown converter used for GW rich-text fields."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from grison.markdown.converter import ConverterError, html_to_md, md_to_html

# --- markdown samples that must round-trip: html_to_md(md_to_html(m)) == m --

MD_SAMPLES = [
    "This is a plain paragraph.",
    "Mix **bold**, `code`, *em*, and a [link](https://example.com/x).",
    "- Item one\n- Item two",
    "First paragraph.\n\nSecond paragraph.",
    "Line one\nLine two",
    # F2: nested inline markup inside a link's visible text.
    "A [**bold link** plain](https://x/) end.",
    # F7: one level of nested list.
    "- parent\n  - child a\n  - child b",
    # ordered lists.
    "1. Item one\n2. Item two",
    "3. Item three\n4. Item four",
    "- top\n  1. child a\n  2. child b",
    # nested-under-ordered needs CommonMark-correct indent width (matches "1. "'s
    # own 3-char width, not a flat 2 spaces) — real CommonMark requires this for
    # the nested list to actually belong to the item rather than becoming a
    # separate top-level list (verified: a real CommonMark previewer agrees).
    "1. top\n   - child a\n   - child b",
]


@pytest.mark.parametrize("md", MD_SAMPLES)
def test_md_round_trips_through_html(md: str) -> None:
    assert html_to_md(md_to_html(md)) == md


# --- html samples that must round-trip (DOM-normalized) --------------------


def _norm(html: str) -> str:
    """Canonicalize an HTML fragment: sorted attrs, collapsed/dropped
    insignificant whitespace. Independent of the converter's own tree builder,
    so it's a real cross-check rather than a tautology."""

    class _Normalizer(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.out: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_str = " ".join(f'{k}="{v}"' for k, v in sorted(attrs))
            self.out.append(f"<{tag} {attr_str}>" if attr_str else f"<{tag}>")

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag: str) -> None:
            self.out.append(f"</{tag}>")

        def handle_data(self, data: str) -> None:
            collapsed = " ".join(data.split())
            if collapsed:
                self.out.append(collapsed)

    parser = _Normalizer()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


# Every <li> is <p>-wrapped — that's the canonical form grison's own md_to_html
# always emits (matches Ghostwriter's real TipTap list-item schema, which always
# contains a block child; see _render_paragraph_node). A bare `<li>text</li>` is
# still accepted as INPUT on the html->md side (it's what a hand-authored/older
# record might contain), but round-tripping through md_to_html canonicalizes it
# to the <p>-wrapped form, so it's not itself a round-trip fixed point.
HTML_SAMPLES = [
    "<p>Plain paragraph.</p>",
    '<p>Mix <strong>bold</strong>, <code>code</code>, <em>em</em> and '
    '<a href="https://example.com/x" target="_blank" rel="noopener">link</a>.</p>',
    "<ul><li><p>Item one</p></li><li><p>Item two</p></li></ul>",
    "<p>Line one<br>Line two</p>",
    # F2: nested inline markup inside a link's visible text — the real CWE-reference
    # idiom present in 11 live GW records (bold label followed by the plain URL).
    '<p>See <a href="https://cwe.mitre.org/data/definitions/122.html" target="_blank" '
    'rel="noopener"><strong>CWE-122: Heap-based Buffer Overflow: </strong>'
    "https://cwe.mitre.org/data/definitions/122.html</a></p>",
    # F2: bold-inside-link, em-inside-link, code-inside-bold — the general nesting case.
    '<p><a href="https://example.com/" target="_blank" rel="noopener">'
    "<em>italic</em> then <strong>bold</strong> link text</a></p>",
    "<p><strong>Bold with <code>inline code</code> and <em>em</em> inside</strong></p>",
    # F7: one level of nested list, single sub-level.
    "<ul><li><p>parent</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ul>",
    # ordered lists.
    "<ol><li><p>Item one</p></li><li><p>Item two</p></li></ol>",
    '<ol start="3"><li><p>Item one</p></li><li><p>Item two</p></li></ol>',
    "<ul><li><p>top</p><ol><li><p>child a</p></li><li><p>child b</p></li></ol></li></ul>",
    "<ol><li><p>top</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ol>",
]


@pytest.mark.parametrize("html", HTML_SAMPLES)
def test_html_round_trips_through_md(html: str) -> None:
    assert _norm(md_to_html(html_to_md(html))) == _norm(html)


def test_span_wrapper_survives_as_inner_text_only() -> None:
    wrapped = '<p>a <span data-color="tomato">wrapped</span> c</p>'
    assert _norm(md_to_html(html_to_md(wrapped))) == _norm("<p>a wrapped c</p>")


# --- on_loss: loud canonicalization warnings (F4, F6) -------------------------


def test_on_loss_reports_dropped_styling_span() -> None:
    events: list[str] = []
    html = '<p>a <span data-color="#ff0000" style="color: #ff0000;">critical</span> b</p>'
    md = html_to_md(html, on_loss=events.append)
    assert md == "a critical b"  # output unchanged — the drop is only made visible
    assert len(events) == 1
    assert "data-color" in events[0] and "style" in events[0]


def test_on_loss_silent_without_callback() -> None:
    # Default behavior is unchanged: no callback given, no exception, span unwrapped.
    assert html_to_md('<p>a <span data-color="red">b</span> c</p>') == "a b c"


def test_on_loss_reports_noncanonical_link_rel_and_target() -> None:
    events: list[str] = []
    html = '<p><a href="https://x/" rel="noopener noreferrer nofollow" target="_self">x</a></p>'
    md = html_to_md(html, on_loss=events.append)
    assert md == "[x](https://x/)"  # output unchanged
    assert any("rel" in e for e in events)
    assert any("target" in e for e in events)


def test_on_loss_silent_for_canonical_link_rel_and_target() -> None:
    events: list[str] = []
    html = '<p><a href="https://x/" rel="noopener" target="_blank">x</a></p>'
    html_to_md(html, on_loss=events.append)
    assert events == []


def test_on_loss_silent_for_link_with_no_rel_or_target() -> None:
    events: list[str] = []
    html_to_md('<p><a href="https://x/">x</a></p>', on_loss=events.append)
    assert events == []


def test_on_loss_reports_dropped_ol_type_attr() -> None:
    events: list[str] = []
    md = html_to_md('<ol type="a"><li>a</li><li>b</li></ol>', on_loss=events.append)
    assert md == "1. a\n2. b"  # output unchanged — always rendered as decimal N.
    assert len(events) == 1
    assert "type" in events[0]


def test_on_loss_silent_for_ol_without_type_attr() -> None:
    events: list[str] = []
    html_to_md('<ol start="2"><li>a</li></ol>', on_loss=events.append)
    assert events == []


# --- fail loud ---------------------------------------------------------------


def test_md_to_html_raises_on_table() -> None:
    with pytest.raises(ConverterError):
        md_to_html("a | b\n---|---\nc | d")


def test_md_to_html_raises_on_image() -> None:
    with pytest.raises(ConverterError):
        md_to_html("![alt text](image.png)")


def test_md_to_html_raises_on_heading() -> None:
    with pytest.raises(ConverterError):
        md_to_html("# Heading")


def test_md_to_html_raises_on_blockquote() -> None:
    with pytest.raises(ConverterError):
        md_to_html("> quoted text")


def test_md_to_html_raises_on_thematic_break() -> None:
    with pytest.raises(ConverterError):
        md_to_html("above\n\n---\n\nbelow")


def test_md_to_html_raises_on_setext_heading() -> None:
    with pytest.raises(ConverterError):
        md_to_html("Title\n===")


def test_md_to_html_raises_on_backtick_fence() -> None:
    with pytest.raises(ConverterError):
        md_to_html("```\ncode\n```")


def test_md_to_html_raises_on_tilde_fence() -> None:
    with pytest.raises(ConverterError):
        md_to_html("~~~\ncode\n~~~")


def test_md_to_html_raises_on_indented_code_block() -> None:
    with pytest.raises(ConverterError):
        md_to_html("    this looks like an indented code block")


def test_md_to_html_raises_on_inline_html() -> None:
    with pytest.raises(ConverterError):
        md_to_html("text <b>bold</b> more")


def test_md_to_html_raises_on_raw_html_block() -> None:
    with pytest.raises(ConverterError):
        md_to_html("<div>raw</div>")


def test_md_to_html_literal_angle_bracket_can_be_escaped() -> None:
    # the fix for the false-positive: escaping makes literal '<' unambiguous.
    assert md_to_html(r"value is \<missing\>") == "<p>value is &lt;missing&gt;</p>"


# --- headings=True (report-narrative mode) ---------------------------------------------------


@pytest.mark.parametrize(
    "md",
    [
        "## Plan",
        "# Top\n\nA paragraph with **bold**.\n\n### Sub\n\n- one\n- two",
        "Intro text.\n\n## Section\n\nBody with `code` and [a](https://x/).",
        "###### Deep\n\nlast",
    ],
)
def test_heading_mode_md_round_trips(md: str) -> None:
    assert html_to_md(md_to_html(md, headings=True), headings=True) == md


def test_heading_mode_html_round_trips() -> None:
    html = "<h2>Plan</h2><p>Five phases:</p><ul><li>recon</li><li>exploit</li></ul><h3>Notes</h3>"
    md = html_to_md(html, headings=True)
    assert md == "## Plan\n\nFive phases:\n\n- recon\n- exploit\n\n### Notes"
    # md is a fixed point across md->html->md (the report merge base relies on this)
    assert html_to_md(md_to_html(md, headings=True), headings=True) == md


def test_headings_still_rejected_in_strict_finding_mode() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<h2>x</h2>")  # default headings=False — the corruption tripwire
    with pytest.raises(ConverterError):
        md_to_html("## x")


# --- ordered lists (<ol>) -----------------------------------------------------
# GW's TinyMCE editor supports ordered lists; the earlier blanket rejection was
# derived from corpus absence, not capability, so <ol> is supported symmetrically
# with <ul> in both directions (never gated by headings=True, same as <ul>).


def test_ol_html_to_md_basic() -> None:
    assert html_to_md("<ol><li>a</li><li>b</li><li>c</li></ol>") == "1. a\n2. b\n3. c"


def test_ol_html_to_md_honors_start_attr() -> None:
    assert html_to_md('<ol start="3"><li>a</li><li>b</li></ol>') == "3. a\n4. b"


def test_ol_html_to_md_numbers_sequentially_regardless_of_source() -> None:
    # <li> carries no per-item number in this vocabulary — numbering always comes
    # from the <ol>'s own position/start, never anything else in the source.
    assert html_to_md("<ol><li>a</li><li>b</li></ol>") == "1. a\n2. b"


def test_ol_md_to_html_basic() -> None:
    assert md_to_html("1. a\n2. b\n3. c") == (
        "<ol><li><p>a</p></li><li><p>b</p></li><li><p>c</p></li></ol>"
    )


def test_ol_md_to_html_no_start_attr_when_starting_at_one() -> None:
    assert "start=" not in md_to_html("1. a\n2. b")


def test_ol_md_to_html_emits_start_attr_when_not_one() -> None:
    assert md_to_html("3. a\n4. b") == '<ol start="3"><li><p>a</p></li><li><p>b</p></li></ol>'


def test_ol_md_to_html_renumbers_on_roundtrip() -> None:
    # Only the FIRST item's literal number is load-bearing (-> start); later
    # numbers are accepted but not otherwise significant — canonical sequential
    # renumbering from start happens when the HTML is read back.
    html = md_to_html("3. a\n7. b\n9. c")
    assert html == '<ol start="3"><li><p>a</p></li><li><p>b</p></li><li><p>c</p></li></ol>'
    assert html_to_md(html) == "3. a\n4. b\n5. c"


def test_ol_bullet_list_unaffected() -> None:
    assert md_to_html("- a\n- b") == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"
    assert html_to_md("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"


def test_ordered_list_accepted_regardless_of_headings_flag() -> None:
    # ol support isn't gated by headings=True (report-narrative mode) — it was a
    # blanket rejection before, now a blanket acceptance, same as <ul>.
    assert md_to_html("1. one\n2. two", headings=True) == (
        "<ol><li><p>one</p></li><li><p>two</p></li></ol>"
    )
    assert html_to_md("<ol><li>one</li></ol>", headings=True) == "1. one"


# --- nested ol/ul mixes --------------------------------------------------------


def test_nested_ol_in_ul_html_to_md() -> None:
    html = "<ul><li>top<ol><li>a</li><li>b</li></ol></li></ul>"
    assert html_to_md(html) == "- top\n  1. a\n  2. b"


def test_nested_ul_in_ol_html_to_md() -> None:
    # Outer marker is "1. " (3 chars) — the nested sub-level's indent on the
    # HTML->markdown side matches that width so grison's OWN output is real,
    # previewable CommonMark (a flat 2 spaces would NOT actually nest when
    # re-read — see _render_list_node).
    html = "<ol><li>top<ul><li>a</li><li>b</li></ul></li></ol>"
    assert html_to_md(html) == "1. top\n   - a\n   - b"


def test_nested_ol_in_ul_html_to_md_honors_nested_start() -> None:
    html = '<ul><li>top<ol start="5"><li>a</li><li>b</li></ol></li></ul>'
    assert html_to_md(html) == "- top\n  5. a\n  6. b"


def test_nested_ol_in_ul_md_to_html_round_trips() -> None:
    md = "- top\n  1. a\n  2. b"
    html = md_to_html(md)
    assert html == "<ul><li><p>top</p><ol><li><p>a</p></li><li><p>b</p></li></ol></li></ul>"
    assert html_to_md(html) == md


def test_nested_ul_in_ol_md_to_html_round_trips() -> None:
    # 3-space indent under "1. " (its own marker width) — 2 spaces would parse as
    # a SEPARATE top-level list under real CommonMark, not a nested one.
    md = "1. top\n   - a\n   - b"
    html = md_to_html(md)
    assert html == "<ol><li><p>top</p><ul><li><p>a</p></li><li><p>b</p></li></ul></li></ol>"
    assert html_to_md(html) == md


# --- round-trip fixed points (load-bearing for the sync engine's merge base) ---


@pytest.mark.parametrize(
    "md",
    [
        "1. a\n2. b\n3. c",
        "3. a\n4. b\n5. c",
        "- top\n  1. a\n  2. b",
        "1. top\n   - a\n   - b",
    ],
)
def test_ol_md_is_fixed_point_through_html(md: str) -> None:
    assert html_to_md(md_to_html(md)) == md


def test_ol_renumbered_output_is_itself_a_fixed_point() -> None:
    # The renumbering canonicalization (see test_ol_md_to_html_renumbers_on_roundtrip)
    # settles after one round trip: applying it again must not drift further.
    once = html_to_md(md_to_html("3. a\n7. b\n9. c"))
    assert once == "3. a\n4. b\n5. c"
    assert html_to_md(md_to_html(once)) == once


def test_html_to_md_raises_on_table() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<table><tr><td>x</td></tr></table>")


def test_html_to_md_raises_on_image() -> None:
    with pytest.raises(ConverterError):
        html_to_md('<img src="x.png">')


def test_html_to_md_raises_on_heading() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<h3>Heading</h3>")


def test_html_to_md_unwraps_span_without_raising() -> None:
    assert html_to_md('<p>a <span style="x">b</span> c</p>') == "a b c"


# --- __strong__, ***strong+em***, link title (defect fixes) ------------------


def test_double_underscore_parses_as_strong() -> None:
    assert md_to_html("__strong text__") == "<p><strong>strong text</strong></p>"


def test_double_underscore_strong_intraword_guard() -> None:
    # snake_case identifiers with double underscores must not be read as markup —
    # same guard as the existing `_em_` intraword rule.
    for text in ("user_id", "snake_case__names", "a__b__c"):
        assert md_to_html(text) == f"<p>{text}</p>"


def test_strong_underscore_round_trips_fixpoint() -> None:
    once = html_to_md(md_to_html("__strong text__"))
    assert once == "**strong text**"  # normalized to ** on the way back — stable-cosmetic
    assert html_to_md(md_to_html(once)) == once  # fixpoint


def test_triple_star_is_strong_and_em() -> None:
    html = md_to_html("***bold and italic***")
    assert html == "<p><strong><em>bold and italic</em></strong></p>"


def test_triple_star_round_trips_fixpoint() -> None:
    md = "***bold and italic***"
    assert html_to_md(md_to_html(md)) == md


def test_link_title_parses_and_round_trips() -> None:
    md = '[text](http://example.com "Title Text")'
    html = md_to_html(md)
    assert html == (
        '<p><a href="http://example.com" title="Title Text" '
        'target="_blank" rel="noopener">text</a></p>'
    )
    assert html_to_md(html) == md


def test_link_without_title_unchanged() -> None:
    md = "[text](http://example.com)"
    html = md_to_html(md)
    assert html == '<p><a href="http://example.com" target="_blank" rel="noopener">text</a></p>'
    assert html_to_md(html) == md


def test_html_link_title_round_trips_to_md_and_back() -> None:
    html = '<p><a href="https://x/" title="See also" target="_blank" rel="noopener">x</a></p>'
    md = html_to_md(html)
    assert md == '[x](https://x/ "See also")'
    assert _norm(md_to_html(md)) == _norm(html)


# --- entity escaping round-trips --------------------------------------------


def test_html_to_md_unescapes_entities() -> None:
    assert html_to_md("<p>a &amp; b &lt; c</p>") == "a & b < c"


def test_md_to_html_escapes_entities() -> None:
    html = md_to_html("a & b < c")
    assert "&amp;" in html
    assert "&lt;" in html


# --- defect fixes (brief: markdown-it rewrite) --------------------------------
# Each test here failed under the old hand-rolled regex tokenizer; see the
# module's git history / the agent report for the exact old (defective) output.


def test_defect_link_url_with_trailing_paren_keeps_full_url() -> None:
    # Old regex-based URL matching stopped at the FIRST ')', truncating the href
    # to "http://example.com/path(1" and leaving a stray ")" as trailing text.
    html = md_to_html("[text](http://example.com/path(1))")
    assert html == (
        '<p><a href="http://example.com/path(1)" target="_blank" '
        'rel="noopener">text</a></p>'
    )


def test_defect_backslash_escaped_asterisk_stays_literal() -> None:
    # Old tokenizer had no backslash-escape concept at all: "\*" would be read
    # character-by-character, and the bare "*" could combine with another "*"
    # elsewhere in the text to form accidental emphasis.
    assert md_to_html(r"\*not emphasis\*") == "<p>*not emphasis*</p>"


def test_defect_backslash_escaped_underscore_and_backtick() -> None:
    assert md_to_html(r"\_not em\_ and \`not code\`") == "<p>_not em_ and `not code`</p>"


def test_defect_hard_break_backslash_syntax_consumed() -> None:
    # Old code split blocks on bare "\n" and joined with "<br>" unconditionally,
    # so a CommonMark backslash-hard-break left the backslash as stray literal
    # text: "line one\\<br>line two" instead of consuming it as the break marker.
    assert md_to_html("line one\\\nline two") == "<p>line one<br>line two</p>"


def test_defect_hard_break_trailing_spaces_syntax_consumed() -> None:
    assert md_to_html("line one  \nline two") == "<p>line one<br>line two</p>"


def test_defect_multi_paragraph_list_item_preserves_paragraph_break() -> None:
    # Old code always joined a multi-<p> <li> onto one space-joined line, losing
    # the paragraph boundary. It now renders as a loose-list continuation.
    html = "<ul><li><p>first step</p><p>then <code>nmap</code></p></li></ul>"
    md = html_to_md(html)
    assert md == "- first step\n\n  then `nmap`"
    assert html_to_md(md_to_html(md)) == md


def test_defect_list_item_with_three_blocks() -> None:
    html = "<ul><li><p>a</p><p>b</p><p>c</p></li></ul>"
    md = html_to_md(html)
    assert md == "- a\n\n  b\n\n  c"
    assert html_to_md(md_to_html(md)) == md


# --- loss visibility: attributes on any allowed tag (not just a/span/ol) -----


def test_on_loss_reports_class_attr_on_link() -> None:
    events: list[str] = []
    html = '<p><a href="https://x/" class="ng-star-inserted">x</a></p>'
    md = html_to_md(html, on_loss=events.append)
    assert md == "[x](https://x/)"
    assert any("class" in e for e in events)


def test_on_loss_reports_style_attr_on_paragraph() -> None:
    events: list[str] = []
    md = html_to_md('<p style="color: red;">hello</p>', on_loss=events.append)
    assert md == "hello"
    assert any("style" in e for e in events)


def test_on_loss_reports_xmlns_attr_on_list() -> None:
    events: list[str] = []
    md = html_to_md(
        '<ul xmlns="http://www.w3.org/1999/xhtml"><li>a</li></ul>', on_loss=events.append
    )
    assert md == "- a"
    assert any("xmlns" in e for e in events)


def test_on_loss_reports_unknown_attr_on_li() -> None:
    events: list[str] = []
    md = html_to_md('<ul><li data-foo="bar">a</li></ul>', on_loss=events.append)
    assert md == "- a"
    assert any("data-foo" in e for e in events)


def test_on_loss_reports_unknown_attr_on_strong() -> None:
    events: list[str] = []
    md = html_to_md('<p><strong class="x">bold</strong></p>', on_loss=events.append)
    assert md == "**bold**"
    assert any("class" in e for e in events)


def test_on_loss_reports_unknown_attr_on_code() -> None:
    events: list[str] = []
    md = html_to_md('<p><code data-lang="bash">ls</code></p>', on_loss=events.append)
    assert md == "`ls`"
    assert any("data-lang" in e for e in events)


def test_on_loss_silent_without_dropped_attrs() -> None:
    events: list[str] = []
    html_to_md("<p>plain</p>", on_loss=events.append)
    assert events == []


# --- block-structure parsing (full-document markdown-it parse) ---------------


def test_lazy_continuation_line_joins_list_item_paragraph() -> None:
    # A list item's paragraph continuation line needs no indent of its own
    # (CommonMark's "lazy continuation") — it still belongs to the item.
    html = md_to_html("- first line\nsecond line lazy")
    assert html == "<ul><li><p>first line<br>second line lazy</p></li></ul>"
    assert html_to_md(html) == "- first line\nsecond line lazy"


def test_list_directly_after_paragraph_no_blank_line_bullet() -> None:
    # A bullet list can interrupt a preceding paragraph with no blank line.
    assert md_to_html("intro\n- a\n- b") == (
        "<p>intro</p>\n\n<ul><li><p>a</p></li><li><p>b</p></li></ul>"
    )


def test_ordered_list_starting_at_one_interrupts_paragraph() -> None:
    assert md_to_html("intro\n1. a\n2. b") == (
        "<p>intro</p>\n\n<ol><li><p>a</p></li><li><p>b</p></li></ol>"
    )


def test_ordered_list_not_starting_at_one_does_not_interrupt_paragraph() -> None:
    # Real CommonMark: an ordered list can only interrupt a paragraph if it
    # starts at 1 — otherwise it's read as a lazy continuation of that paragraph,
    # not a new list. grison must read exactly what a real renderer would show.
    assert md_to_html("intro\n3. a\n4. b") == "<p>intro<br>3. a<br>4. b</p>"


@pytest.mark.parametrize("bullet", ["-", "*", "+"])
def test_bullet_marker_variants_all_accepted(bullet: str) -> None:
    html = md_to_html(f"{bullet} a\n{bullet} b")
    assert html == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"


def test_ordered_marker_paren_variant_accepted() -> None:
    # "1)" is a valid CommonMark ordered-list delimiter, same as "1."
    html = md_to_html("1) a\n2) b")
    assert html == "<ol><li><p>a</p></li><li><p>b</p></li></ol>"


def test_mixed_tight_and_loose_items_in_one_list() -> None:
    # One item single-block (tight), the next multi-block (loose) — both in the
    # same list, each rendered on its own terms.
    md = "- tight item\n- loose item\n\n  second paragraph\n- tight again"
    html = md_to_html(md)
    assert html == (
        "<ul><li><p>tight item</p></li>"
        "<li><p>loose item</p><p>second paragraph</p></li>"
        "<li><p>tight again</p></li></ul>"
    )
    assert html_to_md(html) == md


# --- defect fixes found via property testing (brief follow-up item 1/2) ------


def test_defect_adjacent_same_delimiter_elements_disambiguated() -> None:
    # Two directly-adjacent <strong> elements with nothing between them: naive
    # concatenation ("**a**" + "**b**" = "**a****b**") is read by CommonMark as
    # ONE <strong> spanning "a****b" (the middle 4-star run doesn't split back
    # into a close+open pair). The fix is a tree normalization, not an invented
    # character: the two adjacent <strong> siblings MERGE into one before
    # rendering — visually identical, and reported via on_loss.
    events: list[str] = []
    html = "<p><strong>a</strong><strong>b</strong></p>"
    md = html_to_md(html, on_loss=events.append)
    assert md == "**ab**"
    assert md_to_html(md) == "<p><strong>ab</strong></p>"
    assert any("merged" in e for e in events)


def test_defect_adjacent_code_spans_disambiguated() -> None:
    events: list[str] = []
    html = "<p><code>a</code><code>b</code></p>"
    md = html_to_md(html, on_loss=events.append)
    assert md == "`ab`"
    assert md_to_html(md) == "<p><code>ab</code></p>"
    assert any("merged" in e for e in events)


def test_adjacent_strong_separated_by_whitespace_merges_absorbing_the_space() -> None:
    events: list[str] = []
    html = "<p><strong>a</strong> <strong>b</strong></p>"
    md = html_to_md(html, on_loss=events.append)
    assert md == "**a b**"
    assert md_to_html(md) == "<p><strong>a b</strong></p>"
    assert any("merged" in e for e in events)


def test_three_adjacent_strong_elements_all_merge() -> None:
    html = "<p><strong>a</strong><strong>b</strong><strong>c</strong></p>"
    assert html_to_md(html) == "**abc**"


def test_adjacent_em_and_strong_do_not_merge() -> None:
    # Different tags — never merged, and correctly unambiguous already (**/*
    # use different delimiter characters).
    html = "<p><strong>a</strong><em>b</em></p>"
    assert html_to_md(html) == "**a***b*"
    assert md_to_html("**a***b*") == html


def test_defect_empty_code_span_does_not_corrupt() -> None:
    # `_fence_code("")` used to emit "``" (two backticks, nothing between) —
    # CommonMark reads that as an unclosed opener with no matching closer, so it
    # stays LITERAL "``" text on the next parse instead of vanishing back to
    # nothing, corrupting the round trip (visible text gained two backticks).
    html = "<p><code></code></p>"
    md = html_to_md(html)
    assert md == ""
    assert md_to_html(md) == ""


def test_defect_leading_whitespace_in_html_text_does_not_become_indented_code() -> None:
    # HTML collapses/ignores leading whitespace in normal text flow; grison's
    # OWN markdown output must not preserve it literally, since 4+ leading
    # spaces reads back as an (rejected) indented-code-block attempt on push —
    # exactly backwards for content that was never meant as code.
    html = "<p>    plain text</p>"
    md = html_to_md(html)
    assert not md.startswith("    ")
    assert md_to_html(md) == "<p>plain text</p>"


# --- whitespace-only/empty emphasis: correctly dropped, but reported ---------


def test_whitespace_only_strong_dropped_and_reported() -> None:
    events: list[str] = []
    md = html_to_md("<p><strong> </strong></p>", on_loss=events.append)
    assert md == ""
    assert any("whitespace-only" in e and "strong" in e for e in events)


def test_whitespace_only_em_dropped_and_reported() -> None:
    events: list[str] = []
    md = html_to_md("<p><em> </em></p>", on_loss=events.append)
    assert md == ""
    assert any("whitespace-only" in e and "em" in e for e in events)


def test_empty_strong_dropped_and_reported() -> None:
    events: list[str] = []
    md = html_to_md("<p><strong></strong></p>", on_loss=events.append)
    assert md == ""
    assert any("empty" in e and "strong" in e for e in events)


def test_whitespace_only_strong_in_real_sentence_dropped_and_reported() -> None:
    events: list[str] = []
    md = html_to_md("<p>before<strong> </strong>after</p>", on_loss=events.append)
    assert md == "before after"
    assert any("whitespace-only" in e and "strong" in e for e in events)
