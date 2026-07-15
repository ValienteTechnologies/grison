"""Converter regression tests for the real GW field idioms (synthetic samples).

These pin the constructs found across the live corpus: ``<li><p>…</p></li>`` item
wrapping, nested lists (flattened), shell pipes inside ``<code>`` (not a table), and
the references bullet shape with target/rel/class attrs on the link.
"""

from __future__ import annotations

from grison.markdown import html_to_md, md_to_html


def test_li_unwraps_paragraph() -> None:
    html = "<ul><li><p><strong>CWE-16:</strong> config</p></li></ul>"
    assert html_to_md(html) == "- **CWE-16:** config"


def test_nested_list_is_flattened() -> None:
    html = "<ul><li><p>parent</p><ul><li><p>child a</p></li><li><p>child b</p></li></ul></li></ul>"
    assert html_to_md(html) == "- parent\n- child a\n- child b"


def test_shell_pipe_in_code_is_not_a_table() -> None:
    md = "- Run `head -c 500 /dev/urandom | nc -v host 80`"
    html = md_to_html(md)  # must not raise
    assert "<code>head -c 500 /dev/urandom | nc -v host 80</code>" in html
    assert html_to_md(html) == md  # round-trips


def test_reference_link_idiom_drops_cosmetic_attrs_and_roundtrips() -> None:
    html = (
        '<ul><li><p><strong>CWE-16:</strong> '
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
    html = "<ul><li><p>first step</p><p>then <code>nmap</code></p></li></ul>"
    assert html_to_md(html) == "- first step then `nmap`"


def test_link_url_with_quote_cannot_break_out_of_href() -> None:
    # a malformed/hostile URL must not escape the href attribute and inject markup
    html = md_to_html('see [x](http://evil/a"><img/onerror>)')
    assert "&quot;" in html  # the quote is escaped
    assert '"><' not in html and "<img" not in html  # no attribute breakout / injected tag
