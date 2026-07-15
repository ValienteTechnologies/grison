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


HTML_SAMPLES = [
    "<p>Plain paragraph.</p>",
    '<p>Mix <strong>bold</strong>, <code>code</code>, <em>em</em> and '
    '<a href="https://example.com/x" target="_blank" rel="noopener">link</a>.</p>',
    "<ul><li>Item one</li><li>Item two</li></ul>",
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
    "<ul><li>parent<ul><li>child a</li><li>child b</li></ul></li></ul>",
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


# --- fail loud ---------------------------------------------------------------


def test_md_to_html_raises_on_table() -> None:
    with pytest.raises(ConverterError):
        md_to_html("a | b\n---|---\nc | d")


def test_md_to_html_raises_on_ordered_list() -> None:
    with pytest.raises(ConverterError):
        md_to_html("1. First\n2. Second")


def test_md_to_html_raises_on_image() -> None:
    with pytest.raises(ConverterError):
        md_to_html("![alt text](image.png)")


def test_md_to_html_raises_on_heading() -> None:
    with pytest.raises(ConverterError):
        md_to_html("# Heading")


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


def test_html_to_md_raises_on_table() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<table><tr><td>x</td></tr></table>")


def test_html_to_md_raises_on_ordered_list() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<ol><li>x</li></ol>")


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
