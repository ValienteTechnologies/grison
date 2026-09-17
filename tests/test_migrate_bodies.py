"""Tests for the one-time markdown-body migration
(:func:`grison.migrate.bodies.migrate_body_v1`) — proving the three properties
the brief asks for, using synthetic inputs only (no access to any real
workspace)."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.migrate._v1_converter import ConverterError as V1ConverterError
from grison.migrate._v1_converter import html_to_md as v1_html_to_md
from grison.migrate.bodies import BodyMigrationError, migrate_body_v1


class _TextExtractor(HTMLParser):
    """Strip all tags, decode entities (convert_charrefs=True does this), and
    concatenate every text node in document order — the "what a reader sees"
    invariant migration must never change, regardless of which tag/text node a
    character ends up in (see grison/migrate/bodies.py's documented
    normalizations)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.tags.add(tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible_text(html: str) -> str:
    """What a reader actually sees: HTML collapses/strips insignificant
    whitespace at render time, so this normalizes the same way rather than doing
    a byte-exact text extraction (matches test_converter_property.py's helper)."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    return " ".join(text.split())


def tags_used(html: str) -> set[str]:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return p.tags - {"p"}  # <p>-wrapping is a documented, text-preserving normalization


# --- property (a): visible text never changes, only documented whitespace moves ---

V1_MARKDOWN_SAMPLES = [
    "Normal paragraph, nothing weird here.",
    "plain & ampersand test",
    "- item one\n- item two",
    "1. item one\n2. item two",
    # v1's own bold/em whitespace-placement defect (trailing space inside the
    # delimiter, which real CommonMark's flanking rule won't parse as emphasis
    # once re-read raw — migration reconstructs the ORIGINAL HTML instead).
    "**CWE-120: **[link](http://example.com/x)",
    "**Trailing space bold **and more text",
    "*em trailing space *and more",
    "__leading__ **and** *mixed* `code` text",
    # v1 never recognized inline HTML at all — always HTML-escaped literal
    # angle-bracket text on push; the new converter's real CommonMark parser
    # would misread/reject these RAW, which is exactly what migration fixes.
    "Some text with <iframe src=x></iframe> literally.",
    "Check for <script>alert(1)</script> injection.",
    "connect to <domain> as <user>",
    "compare a < b and b > c values",
]


@pytest.mark.parametrize("x", V1_MARKDOWN_SAMPLES)
def test_migrated_body_has_same_visible_text_as_old_push(x: str) -> None:
    from grison.migrate._v1_converter import md_to_html as v1_md_to_html

    h_old = v1_md_to_html(x)
    migrated = migrate_body_v1(x)
    h_new = md_to_html(migrated)
    assert visible_text(h_new) == visible_text(h_old), (
        f"visible text changed: old={visible_text(h_old)!r} new={visible_text(h_new)!r}"
    )


# --- property (b): idempotent on already-valid CommonMark from the NEW html_to_md -


ALREADY_CANONICAL_HTML_SAMPLES = [
    "<p>Plain <strong>bold</strong> and <em>em</em> text.</p>",
    "<ul><li><p>a</p></li><li><p>b</p></li></ul>",
    '<p>A <a href="http://x/" target="_blank" rel="noopener">link</a>.</p>',
    "<ol><li><p>one</p></li><li><p>two</p></li></ol>",
    "<p>Code: <code>a | nc</code> and more.</p>",
]


@pytest.mark.parametrize("html", ALREADY_CANONICAL_HTML_SAMPLES)
def test_migration_is_idempotent_on_already_canonical_markdown(html: str) -> None:
    canonical_md = html_to_md(html)
    assert migrate_body_v1(canonical_md) == canonical_md


def test_migration_idempotent_twice_in_a_row() -> None:
    md = "Plain **bold** and *em* text with a [link](http://x/)."
    once = migrate_body_v1(md)
    twice = migrate_body_v1(once)
    assert twice == once


# --- property (c): the four concrete shapes read back as the same visible text ---


def test_bold_trailing_space_shape_visible_text_preserved() -> None:
    x = "**CWE-120: **[link](http://example.com/x)"
    migrated = migrate_body_v1(x)
    html = md_to_html(migrated)
    assert visible_text(html) == "CWE-120: link"
    assert "strong" in tags_used(html) and "a" in tags_used(html)


def test_literal_iframe_shape_visible_text_preserved() -> None:
    x = "Some text with <iframe src=x></iframe> literally."
    migrated = migrate_body_v1(x)
    html = md_to_html(migrated)
    assert visible_text(html) == "Some text with <iframe src=x></iframe> literally."
    # and, critically, it's inert — no real <iframe> tag in the output
    assert "iframe" not in tags_used(html)


def test_literal_script_like_words_shape_visible_text_preserved() -> None:
    x = "Check for <script>alert(1)</script> injection."
    migrated = migrate_body_v1(x)
    html = md_to_html(migrated)
    assert visible_text(html) == "Check for <script>alert(1)</script> injection."
    assert "script" not in tags_used(html)


def test_bare_comparison_operators_shape_visible_text_preserved() -> None:
    x = "compare a < b and b > c values"
    migrated = migrate_body_v1(x)
    html = md_to_html(migrated)
    assert visible_text(html) == "compare a < b and b > c values"


def test_domain_placeholder_shape_visible_text_preserved() -> None:
    x = "connect to <domain> as <user>"
    migrated = migrate_body_v1(x)
    html = md_to_html(migrated)
    assert visible_text(html) == "connect to <domain> as <user>"
    assert "domain" not in tags_used(html) and "user" not in tags_used(html)


# --- feeding raw (unmigrated) v1 markdown to the NEW converter directly: this is
# exactly the failure migration exists to avoid — proves the "before" state ------


def test_raw_v1_markdown_with_iframe_shape_is_rejected_by_new_converter_directly() -> None:
    # Without migration, the new converter reads a real <iframe>-shaped run as
    # inline HTML and hard-rejects it — this is NOT a converter bug (D1: "still
    # rejected: raw HTML blocks and inline HTML"), it's exactly why migration
    # reconstructs the original HTML instead of re-parsing v1's raw markdown text.
    with pytest.raises(ConverterError):
        md_to_html("Some text with <iframe src=x></iframe> literally.")


# --- BodyMigrationError: clear failure, not silent/partial ---------------------


def test_migrate_body_v1_wraps_old_side_failure() -> None:
    # A fenced code block is unsupported by v1's own md_to_html.
    with pytest.raises(BodyMigrationError):
        migrate_body_v1("```\ncode\n```")


def test_migrate_body_v1_empty_input_is_empty_output() -> None:
    assert migrate_body_v1("") == ""
    assert migrate_body_v1("   \n  ") == ""


def test_migrate_body_v1_headings_flag_forwarded() -> None:
    migrated = migrate_body_v1("## Section\n\nBody text.", headings=True)
    assert migrated == "## Section\n\nBody text."
    with pytest.raises(BodyMigrationError):
        migrate_body_v1("## Section", headings=False)


# --- property: migrate_body_v1's output is an immediate fixpoint for the NEW
# converter, even though the input it's built from is genuine v1-DIALECT
# markdown (v1's own html_to_md output, not hand-written author markdown) —
# this is what actually caught three real non-fixpoint bugs against a real
# 1,178-field production-shaped workspace (adjacent v1-produced <ul> blocks
# merging into one loose list on re-parse; a trailing space before a
# newline immediately followed by a list-looking line; two spaces after a
# bullet marker) that the markdown-first, HTML-first property tests in
# test_converter_property.py never reached, because those never generate
# v1's OWN whitespace/list-rendering quirks — only v1's real
# implementation does. See grison/migrate/bodies.py's module docstring for
# why migrate_body_v1 exists at all.

_MIGRATE_WORDS = ["plain", "word-one", "CWE-120", "a<b", "under_score", "star*text"]


def _esc(w: str) -> str:
    return w.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@st.composite
def _gw_inline(draw: st.DrawFn, depth: int = 0, exclude: frozenset[str] = frozenset()) -> str:
    """A small GW-shaped inline-HTML generator, self-contained (no cross-import
    from test_converter_property.py, to keep this file's generators
    independent) — text/strong/em/code/a/span, enough to exercise v1's inline
    rendering without chasing every corner test_converter_property.py already
    covers for the NEW converter directly. Same-tag direct nesting
    (``<strong>`` directly around another ``<strong>``, ``<em>`` around
    ``<em>``) is excluded via ``exclude`` for the same reason
    test_converter_property.py's own ``gw_inline_html`` avoids it: no real
    Ghostwriter/TinyMCE editor produces it via its toolbar, and it's a
    genuine CommonMark flanking-rule representability gap once such a run
    sits next to other content — not a grison bug this property is about."""
    if depth >= 2:
        return _esc(draw(st.sampled_from(_MIGRATE_WORDS)))
    choices = [c for c in ["text", "text", "strong", "em", "code", "a", "span"] if c not in exclude]
    kind = draw(st.sampled_from(choices))
    if kind == "text":
        return _esc(draw(st.sampled_from(_MIGRATE_WORDS)))
    if kind == "strong":
        return f"<strong>{draw(_gw_inline(depth + 1, exclude=frozenset({'strong'})))}</strong>"
    if kind == "em":
        return f"<em>{draw(_gw_inline(depth + 1, exclude=frozenset({'em'})))}</em>"
    if kind == "code":
        return f"<code>{_esc(draw(st.sampled_from(_MIGRATE_WORDS)))}</code>"
    if kind == "a":
        return f'<a href="http://example.com/x">{draw(_gw_inline(depth + 1))}</a>'
    return f"<span>{draw(_gw_inline(depth + 1, exclude=exclude))}</span>"


@st.composite
def _gw_paragraph(draw: st.DrawFn) -> str:
    n = draw(st.integers(min_value=1, max_value=3))
    inner = " ".join(draw(_gw_inline()) for _ in range(n))
    if not inner.strip():
        inner = "x" + inner
    return f"<p>{inner}</p>"


@st.composite
def _gw_list(draw: st.DrawFn) -> str:
    """A <ul>/<ol> block with 1-3 <li> items, each either bare inline content
    or a <p>-wrapped one — deliberately including the shape that reproduces
    "two adjacent same-tag lists merge into one on re-parse" by sometimes
    drawing TWO list blocks back to back with nothing but whitespace between
    them (see the docstring above, shape 1)."""
    tag = draw(st.sampled_from(["ul", "ol"]))
    n_items = draw(st.integers(min_value=1, max_value=3))
    items = []
    for _ in range(n_items):
        content = draw(_gw_inline())
        if draw(st.booleans()):
            content = f"<p>{content}</p>"
        items.append(f"<li>{content}</li>")
    block = f"<{tag}>{''.join(items)}</{tag}>"
    if draw(st.booleans()):
        # a second, adjacent list of the SAME tag right after it
        n_items2 = draw(st.integers(min_value=1, max_value=2))
        items2 = [f"<li>{draw(_gw_inline())}</li>" for _ in range(n_items2)]
        block += f"<{tag}>{''.join(items2)}</{tag}>"
    return block


@st.composite
def gw_document_html(draw: st.DrawFn) -> str:
    n_blocks = draw(st.integers(min_value=1, max_value=3))
    blocks = []
    for _ in range(n_blocks):
        if draw(st.booleans()):
            blocks.append(draw(_gw_list()))
        else:
            blocks.append(draw(_gw_paragraph()))
    return "".join(blocks)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw_document_html())
def test_migrate_body_v1_output_is_an_immediate_fixpoint(html: str) -> None:
    """x is genuine v1-dialect markdown (the frozen old html_to_md's own
    output for generated GW-shaped HTML, including list structure) — not
    hand-written author markdown. m = migrate_body_v1(x) must then be an
    immediate fixpoint under the NEW converter in both directions, exactly
    as the brief requires."""
    try:
        x = v1_html_to_md(html)
    except V1ConverterError:
        return  # outside v1's own supported grammar — not this property's concern
    try:
        m = migrate_body_v1(x)
    except BodyMigrationError:
        return  # a real, reported failure — not a silent/partial result either way
    if not m.strip():
        return

    r1 = html_to_md(md_to_html(m))
    assert r1 == m, f"non-fixpoint: html={html!r} x={x!r} m={m!r} r1={r1!r}"

    h = md_to_html(m)
    h2 = md_to_html(html_to_md(h))
    assert h2 == h, f"non-fixpoint (html side): html={html!r} m={m!r} h={h!r} h2={h2!r}"
