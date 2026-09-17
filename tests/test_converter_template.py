"""Tests for D10 (template delimiters) — literal ``{{ }}``/``{% %}``/``{# #}`` in
author text vs. an ACTIVE (un-escaped) Jinja expression found in pulled HTML. See
the module docstring for the chosen escape form (a Jinja string-literal
expression per delimiter TOKEN, e.g. ``{%`` -> ``{{ '{%' }}`` — deliberately not
pair-based, unlike an earlier design that wrapped matched
``{{...}}``/``{%...%}``/``{#...#}`` spans in Jinja's own
``{% raw %}...{% endraw %}`` block tag, since pairing can't neutralize a lone,
never-closed opener — verified against a real Ghostwriter 7.2.6 lab server, see
``/home/tfp/repos/grison-rework/proofs/d10-jinja-escape-lab.md``) and the
reserved ``gw:`` inline-code form for active expressions.
"""

from __future__ import annotations

import re

import pytest

from grison.markdown.converter import html_to_md, md_to_html

# --- literal braces in author text: escaped on push, restored on pull --------

# Matches grison's own D10 escape form so it can be stripped out below, leaving
# only whatever the converter DIDN'T recognize as needing escaping — used to
# prove no bare, live-triggering delimiter token is left anywhere in the output.
_STRLIT_ESCAPE_RE = re.compile(r"\{\{\s*'(?:\{\{|\}\}|\{%|%\}|\{#|#\})'\s*\}\}")
_RAW_TOKENS = ("{{", "}}", "{%", "%}", "{#", "#}")


def _bare_jinja_tokens(html: str) -> list[str]:
    """Every occurrence of a real Jinja delimiter token in ``html`` that is
    NOT part of grison's own escape form — i.e. every token Jinja's real
    lexer would still treat as live/triggering once grison is done with it.
    An empty list is the whole point of D10."""
    stripped = _STRLIT_ESCAPE_RE.sub("", html)
    return [tok for tok in _RAW_TOKENS if tok in stripped]


@pytest.mark.parametrize(
    "text",
    [
        "Say {{ client.name }} literally.",
        "A statement {% if x %} literally.",
        "A comment {# not really a comment #} literally.",
        "Multiple {{ a }} and {% b %} and {# c #} in one line.",
    ],
)
def test_literal_delimiters_escaped_on_push_and_restored_on_pull(text: str) -> None:
    html = md_to_html(text)
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == text
    # fixpoint: escaping settles after one round
    assert html_to_md(md_to_html(html_to_md(html))) == text


def test_literal_delimiters_escaped_inside_inline_code() -> None:
    html = md_to_html("run `echo {{ not_a_var }}` now")
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == "run `echo {{ not_a_var }}` now"


def test_jinja_escape_false_does_not_wrap_literal_braces() -> None:
    html = md_to_html("Say {{ client.name }} literally.", jinja_escape=False)
    assert _bare_jinja_tokens(html) == ["{{", "}}"]  # not escaped at all, by request
    assert "{{ client.name }}" in html


# --- item C: lone/unmatched openers, nested/overlapping delimiters, a split
# across an inline tag boundary, and literal "{% raw %}"/"{% endraw %}" author
# text — none of these may leave a live, un-neutralized delimiter token behind.


def test_lone_unclosed_opener_is_still_escaped() -> None:
    # "{{" here is never closed anywhere in the field — a pair-based escape
    # (matching {{...}}) would miss this entirely and leave it live.
    text = "Only an opener {{ here, never closed."
    html = md_to_html(text)
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == text


def test_lone_unclosed_block_opener_is_still_escaped() -> None:
    text = "A statement {% if x, no closing tag."
    html = md_to_html(text)
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == text


def test_nested_overlapping_delimiters_all_escaped() -> None:
    text = "Mix {{ {% }} together."
    html = md_to_html(text)
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == text
    assert html_to_md(md_to_html(html_to_md(html))) == text


def test_delimiter_split_across_an_inline_tag_boundary_stays_safe() -> None:
    # "{" ends up right before a <strong> tag, and the matching "%" (part of
    # what WOULD read as "{%" if the two were literally adjacent) sits INSIDE
    # it — genuinely not adjacent in the rendered HTML (a real <strong> tag
    # sits between them), so this is safe by construction; this test locks
    # that in rather than papering over a gap.
    text = "a{**%**}b"
    html = md_to_html(text)
    assert "<strong>%</strong>" in html
    assert _bare_jinja_tokens(html) == []
    assert html_to_md(html) == text
    assert html_to_md(md_to_html(html_to_md(html))) == text


def test_literal_raw_endraw_author_text_not_eaten_by_own_unwrap() -> None:
    # The OLD design's own {% raw %}...{% endraw %} escape form would have
    # been indistinguishable from an author literally typing this text; the
    # new per-token design has no special case for "raw"/"endraw" at all, so
    # this just round-trips like any other literal-brace text.
    text = "Please use {% raw %} and {% endraw %} exactly as shown."
    html = md_to_html(text)
    assert _bare_jinja_tokens(html) == []
    assert "{% raw %}" not in html  # the literal 9-char run itself doesn't survive intact
    assert "{% endraw %}" not in html
    assert html_to_md(html) == text


def test_literal_wrapper_unwraps_without_on_loss_noise() -> None:
    events: list[str] = []
    html = md_to_html("Say {{ x }} literally.")
    html_to_md(html, on_loss=events.append)
    assert events == []  # D10 escaping is not itself a "loss" — content is preserved


# --- active (un-wrapped) expressions found in pulled HTML: preserved, not eaten -


def test_active_expression_preserved_as_reserved_inline_code() -> None:
    html = "<p>Hello {{ client.name }}, welcome.</p>"
    md = html_to_md(html)
    assert md == "Hello `gw:{{ client.name }}`, welcome."


@pytest.mark.parametrize(
    "html,expected_md",
    [
        ("<p>{{ client.name }}</p>", "`gw:{{ client.name }}`"),
        ("<p>{% if x %}</p>", "`gw:{% if x %}`"),
        ("<p>{# a comment #}</p>", "`gw:{# a comment #}`"),
    ],
)
def test_all_three_active_delimiter_forms_preserved(html: str, expected_md: str) -> None:
    assert html_to_md(html) == expected_md


def test_active_expression_round_trips_to_identical_html() -> None:
    html = "<p>Hello {{ client.name }}, welcome.</p>"
    md = html_to_md(html)
    assert md_to_html(md) == html
    # canonical stability: md_to_html(html_to_md(h)) == h for h produced this way
    assert md_to_html(html_to_md(md_to_html(md))) == md_to_html(md)


def test_active_expression_not_escaped_even_with_jinja_escape_true() -> None:
    # the reserved gw: form bypasses D10 escaping entirely — it's already known
    # to be an active expression, not literal author text.
    html = md_to_html("`gw:{{ client.name }}`", jinja_escape=True)
    assert "data-gw-jinja-literal" not in html
    assert html == "<p>{{ client.name }}</p>"


# --- proof: escaped output survives Ghostwriter's REAL Jinja pipeline --------
# html_rich_text.py's rich_text_template(), in order: (1) extract elements
# carrying literal fragments to opaque placeholders, (2) legacy {{.x}} dot-syntax
# rewrite, (3) TinyMCE pagebreak comment rewrite, (4) _process_prefix, (5)
# compile+render under a ReportSandboxedEnvironment(autoescape=True) (a
# jinja2.sandbox.ImmutableSandboxedEnvironment subclass), (6) restore any
# placeholders. Since D10's escape form (a Jinja string-literal expression per
# delimiter token — see module docstring) is plain, ordinary Jinja expression
# syntax rather than a custom extraction target, NONE of that machinery is
# needed here at all — jinja2's real parser handles ``{{ '...' }}`` natively,
# so this mimic is just "compile and render the HTML directly," which is
# exactly what a real ``ReportSandboxedEnvironment`` does once none of steps
# (1)-(4) apply (no dot-syntax/pagebreak/prefix markers in this test content).
# Confirmed for real against the lab server, including the lone-opener/
# nested-overlapping/literal-raw-endraw-text cases below — see
# ``/home/tfp/repos/grison-rework/proofs/d10-jinja-escape-lab.md``.


def _mimic_ghostwriter_render(html: str, context: dict | None = None) -> str:
    """Compile+render ``html`` as a Jinja template under a real jinja2
    SandboxedEnvironment (jinja2 is not a grison dependency; Ghostwriter's own
    ``ReportSandboxedEnvironment`` is a thin, additionally-locked-down subclass of
    this same base — see ``ghostwriter/modules/reportwriter/__init__.py``). Only
    called from the two tests below, each of which does its own ``importorskip``
    first, so importing jinja2 here (module-level would skip the WHOLE file) is
    safe."""
    import jinja2.sandbox

    env = jinja2.sandbox.SandboxedEnvironment(autoescape=True)
    return env.from_string(html).render(context or {})


def test_escaped_literal_delimiters_survive_real_jinja_sandboxed_render() -> None:
    pytest.importorskip("jinja2")
    text = "Say {{ client.name }} and {% if x %} and {# a comment #} literally."
    html = md_to_html(text)
    rendered = _mimic_ghostwriter_render(html)
    # Jinja never saw the delimiters as delimiters (each was neutralized as a
    # string-literal expression before compilation) — they come back exactly
    # as literal text, not evaluated, not stripped.
    assert "{{ client.name }}" in rendered
    assert "{% if x %}" in rendered
    assert "{# a comment #}" in rendered
    assert "Say" in rendered and "literally." in rendered


@pytest.mark.parametrize(
    "text,visible_text",
    [
        ("Only an opener {{ here, never closed.", "Only an opener {{ here, never closed."),
        ("Mix {{ {% }} together.", "Mix {{ {% }} together."),
        (
            "Please use {% raw %} and {% endraw %} exactly as shown.",
            "Please use {% raw %} and {% endraw %} exactly as shown.",
        ),
        ("a{**%**}b", "a{%}b"),  # split across an inline tag boundary (bold survives as tags)
    ],
)
def test_item_c_delimiter_shapes_survive_real_jinja_sandboxed_render(
    text: str, visible_text: str
) -> None:
    pytest.importorskip("jinja2")
    html = md_to_html(text)
    rendered = _mimic_ghostwriter_render(html)
    # Recovering the exact original author text out of Jinja's real renderer
    # (stripped of grison's own <p>/<strong> HTML wrapping) is the strongest
    # possible proof: nothing was evaluated, nothing raised, nothing left a
    # dangling/unterminated tag for the compiler to choke on.
    from html.parser import HTMLParser

    class _Strip(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    p = _Strip()
    p.feed(rendered)
    p.close()
    assert "".join(p.parts) == visible_text, f"real Jinja render diverged: {rendered!r}"


def test_active_expression_left_by_grison_actually_executes_in_real_jinja() -> None:
    pytest.importorskip("jinja2")
    # The other half of the proof: an expression grison preserves as ACTIVE (the
    # gw: reserved form, un-escaped on push) must still be a genuine, executable
    # Jinja expression once it reaches Ghostwriter's real renderer — not just
    # "not neutralized" but actually live.
    md = "Hello `gw:{{ client.name }}`, welcome."
    html = md_to_html(md)
    rendered = _mimic_ghostwriter_render(html, {"client": {"name": "Acme Corp"}})
    assert rendered == "<p>Hello Acme Corp, welcome.</p>"
