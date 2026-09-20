"""Converter regression tests for the real GW field idioms (synthetic samples).

These pin the constructs found across the live corpus: ``<li><p>…</p></li>`` item
wrapping, one level of nested-list support (2-space ``  - `` sub-bullets, deeper
nesting degraded to that same sub-level), shell pipes inside ``<code>`` (not a
table), and the references bullet shape with target/rel/class attrs on the link.
Ordered-list (``<ol>``) cases exercise the same ``<li><p>…</p></li>`` idiom GW uses
for ``<ul>`` — TinyMCE supports ``<ol>`` even though the live corpus, sampled
before this support was added, happens not to contain one yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grison.markdown import ConverterError, html_to_md, md_to_html

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "lab-samples"
_FINDING_SECTIONS = ("description", "impact", "mitigation", "replication_steps", "references")


def test_li_unwraps_paragraph() -> None:
    html = "<ul><li><p><strong>CWE-16:</strong> config</p></li></ul>"
    assert html_to_md(html) == "- **CWE-16:** config"


def test_nested_list_renders_as_indented_sub_bullets() -> None:
    # Was flattened to sibling bullets; now pins the 2-space nested convention (F7).
    html = "<ul><li><p>parent</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ul>"
    md = html_to_md(html)
    assert md == "- parent\n  - child a\n  - child b"
    # md -> html -> md is a fixed point (the merge base relies on this)
    assert html_to_md(md_to_html(md)) == md


def test_three_level_nesting_degrades_to_one_sub_level() -> None:
    # A <ul> nested inside a nested <li> (3 levels deep) collapses into the SAME
    # single sub-level rather than growing a third indent — documented, deliberate.
    html = "<ul><li><p>a</p><ul><li><p>b</p><ul><li><p>c</p></li></ul></li></ul></li></ul>"
    assert html_to_md(html) == "- a\n  - b\n  - c"


def test_three_level_nesting_collapse_reports_on_loss() -> None:
    events: list[str] = []
    html = "<ul><li><p>a</p><ul><li><p>b</p><ul><li><p>c</p></li></ul></li></ul></li></ul>"
    html_to_md(html, on_loss=events.append)
    assert any("collapsed" in e and "sub-level" in e for e in events)


def test_authoring_a_third_list_level_is_a_hard_error() -> None:
    # The markdown side has no way to express a 3rd level at all — unlike the
    # html->md side (which may see one in existing GW data and collapses it),
    # freshly-authored markdown attempting one is a hard, loud rejection.
    with pytest.raises(ConverterError):
        md_to_html("- a\n  - b\n    - c")


def test_md_nested_bullets_round_trip_to_html_and_back() -> None:
    md = "- parent\n  - child a\n  - child b"
    html = md_to_html(md)
    assert html == (
        "<ul><li><p>parent</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ul>"
    )
    assert html_to_md(html) == md


def test_shell_pipe_in_code_is_not_a_table() -> None:
    md = "- Run `head -c 500 /dev/urandom | nc -v host 80`"
    html = md_to_html(md)  # must not raise
    assert "<code>head -c 500 /dev/urandom | nc -v host 80</code>" in html
    assert html_to_md(html) == md  # round-trips


def test_reference_link_idiom_drops_cosmetic_attrs_and_roundtrips() -> None:
    html = (
        "<ul><li><p><strong>CWE-16:</strong> "
        '<a target="_blank" rel="noopener" class="ng-star-inserted" '
        'href="https://cwe.mitre.org/data/definitions/16.html">'
        "https://cwe.mitre.org/data/definitions/16.html</a></p></li></ul>"
    )
    md = html_to_md(html)
    assert md == (
        "- **CWE-16:** [https://cwe.mitre.org/data/definitions/16.html]"
        "(https://cwe.mitre.org/data/definitions/16.html)"
    )
    # md -> html re-adds the canonical target/rel; re-reading is stable
    assert html_to_md(md_to_html(md)) == md


def test_multi_paragraph_li_joins() -> None:
    # CHANGED behavior (defect fix, brief item 1): a multi-paragraph/"loose" list
    # item used to be lossily joined onto one line with a space, destroying the
    # paragraph break. It now renders as a loose-list continuation — a blank line
    # then a 2-space-indented block — matching GW's own <li><p>...</p><p>...</p>
    # shape without collapsing it.
    html = "<ul><li><p>first step</p><p>then <code>nmap</code></p></li></ul>"
    md = html_to_md(html)
    assert md == "- first step\n\n  then `nmap`"
    # md -> html -> md is a fixed point (the merge base relies on this)
    assert html_to_md(md_to_html(md)) == md


def test_link_url_with_quote_cannot_break_out_of_href() -> None:
    # CHANGED behavior (markdown-it now owns URL parsing): a malformed/hostile URL
    # must still not escape the href attribute and inject markup, but it's now
    # prevented by markdown-it's own URL normalization (percent-encoding unsafe
    # characters) rather than by grison's own quote-escaping seeing a literal `"`
    # reach the attribute — there's no literal quote left to escape.
    html = md_to_html('see [x](http://evil/a"><img/onerror>)')
    assert '"><' not in html and "<img" not in html  # no attribute breakout / injected tag
    assert "%22" in html  # the quote was neutralized by percent-encoding, not dropped


# --- ordered lists (<ol>), same GW <li><p>…</p></li> item wrapping as <ul> -----


def test_ol_li_unwraps_paragraph() -> None:
    html = "<ol><li><p>first</p></li><li><p>second</p></li></ol>"
    assert html_to_md(html) == "1. first\n2. second"


def test_ol_nested_in_ul_renders_as_indented_sub_items() -> None:
    html = "<ul><li><p>parent</p><ol><li><p>child a</p></li><li><p>child b</p></li></ol></li></ul>"
    md = html_to_md(html)
    assert md == "- parent\n  1. child a\n  2. child b"
    # md -> html -> md is a fixed point (the merge base relies on this)
    assert html_to_md(md_to_html(md)) == md


def test_ul_nested_in_ol_renders_as_indented_sub_items() -> None:
    # Outer marker "1. " is 3 chars — the nested sub-level's indent matches that
    # width (not a flat 2 spaces), so grison's own output is real, previewable
    # CommonMark (see _render_list_node).
    html = "<ol><li><p>parent</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ol>"
    md = html_to_md(html)
    assert md == "1. parent\n   - child a\n   - child b"
    assert html_to_md(md_to_html(md)) == md


def test_three_level_nesting_with_mixed_ol_ul_degrades_to_one_sub_level() -> None:
    # A <ul> nested three levels deep (inside an <ol> nested inside a <ul>) collapses
    # into the SAME single sub-level as the 2nd-level <ol> — only the indent
    # collapses, each contributing list keeps its own marker style.
    html = "<ul><li><p>a</p><ol><li><p>b</p><ul><li><p>c</p></li></ul></li></ol></li></ul>"
    assert html_to_md(html) == "- a\n  1. b\n  - c"


# --- real GW 7.2.6 finding fixtures: never raise, always reach a stable fixpoint -
# ``gw-findings.json`` is a real Ghostwriter 7.2.6 export sample (see
# ``engine-findings-lab.md``'s "Two defects", #1: this fixture's finding 3
# description, ``<h3>Overview</h3><p>...</p>``, is the exact payload that used to
# raise ``ConverterError: unsupported HTML tag: <h3>`` before the finding
# adapters started passing ``headings=True`` — see
# ``grison.markdown.converter``'s module docstring). ``headings=True`` matches
# ``grison.adapters.gw_findings``' own call sites exactly.


def _finding_field_htmls() -> list[tuple[str, str]]:
    data = json.loads((_FIXTURES_DIR / "gw-findings.json").read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for row in data["data"]["finding"]:
        for field in _FINDING_SECTIONS:
            html = row.get(field) or ""
            if html.strip():
                out.append((f"finding {row['id']} {field}", html))
    return out


@pytest.mark.parametrize(
    "html",
    [h for _, h in _finding_field_htmls()],
    ids=[label for label, _ in _finding_field_htmls()],
)
def test_real_gw_finding_fixture_never_raises_and_reaches_immediate_fixpoint(html: str) -> None:
    md = html_to_md(html, headings=True)  # must not raise (defect 1's regression, corpus-wide)
    md2 = html_to_md(md_to_html(md, headings=True), headings=True)
    assert md2 == md, f"non-fixpoint: html={html!r} md={md!r} md2={md2!r}"
