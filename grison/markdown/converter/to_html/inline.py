"""markdown->html: inline-content rendering. Converts markdown-it's flat inline
token list into the SAME ``_Node``/str tree shape the html->md side builds
from real HTML (:func:`_md_to_seminode`), so the proven adjacency-merge
normalization (:mod:`grison.markdown.converter.inline_normalize`) applies to
markdown-it-sourced content too, then renders that tree to HTML
(:func:`_render_seminode_html`).
"""

from __future__ import annotations

import re

from markdown_it.tree import SyntaxTreeNode

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.grammar import _UNRESOLVED_RE
from grison.markdown.converter.inline_normalize import _merge_adjacent_inline
from grison.markdown.converter.jinja import _jinja_escape_html
from grison.markdown.converter.mdtext import _esc
from grison.markdown.converter.nodes import _Node
from grison.markdown.converter.to_html.evidence import _push_cross_ref, _render_unresolved_marker
from grison.markdown.refs import RefResolver

_GW_ACTIVE_EXPR_RE = re.compile(r"^gw:(?P<expr>\{\{.*\}\}|\{%.*%\}|\{#.*#\})$", re.DOTALL)


def _md_to_seminode(
    nodes: list[SyntaxTreeNode], refs: RefResolver | None, jinja_escape: bool, line: int
) -> list[_Node | str]:
    """Convert markdown-it's flat inline node list into grison's OWN ``_Node``/
    str tree (the SAME representation the html->md side builds from real HTML),
    resolving the ``***text***`` em(strong)->strong(em) swap immediately (so the
    SAME tag applies regardless of source shape) — so the ALREADY-PROVEN
    ``_merge_adjacent_inline`` normalization (shared with html_to_md, see its
    docstring) can run on markdown-it-sourced content too, recursively and
    correctly: reusing it here (rather than a second, string-surgery-based
    merge mechanism) is what makes a CASCADING case work — e.g. two adjacent
    ``***a*** ***b***`` runs merge at the outer ``<strong>`` level, which then
    exposes their INNER ``<em>a</em>``/``<em>b</em>`` as newly-adjacent
    siblings needing a SECOND merge too; ``_merge_adjacent_inline`` already
    handles that because ``_render_seminode_html`` re-invokes it every time it
    recurses into a node's (possibly just-combined) children, exactly like
    ``_render_inline`` already does on the html->md side.

    A link, an active/unresolved ``gw:`` code marker, or anything else with its
    own specialized render path is fully rendered to a final HTML string
    immediately and wrapped as an opaque ``_Node("_raw", ..., [html])`` leaf —
    its own inner content already went through this same merge machinery via
    its own render call, so nothing is lost, and it's never itself a merge
    target (a link's ``[`` immediately makes any adjacent same-tag concern
    moot)."""
    out: list[_Node | str] = []
    for n in nodes:
        if n.type == "text":
            out.append(n.content)
        elif n.type in ("softbreak", "hardbreak"):
            out.append(_Node("br"))
        elif n.type == "strong":
            out.append(_Node("strong", {}, _md_to_seminode(n.children, refs, jinja_escape, line)))
        elif n.type == "em":
            # ***text*** parses as em(strong(text)) under real CommonMark delimiter
            # resolution; grison's canonical order is <strong><em> (the html->md
            # side never emits the other order) — swap when an em's ENTIRE content
            # is a single strong with nothing else (the only shape a "***"/"___"
            # run can produce; a genuinely mixed nest like "*a **b** c*" has other
            # content in the em besides the strong, so it's left alone).
            inner_nodes = [c for c in n.children if not (c.type == "text" and c.content == "")]
            if len(inner_nodes) == 1 and inner_nodes[0].type == "strong":
                em_wrapped = _Node(
                    "em", {}, _md_to_seminode(inner_nodes[0].children, refs, jinja_escape, line)
                )
                out.append(_Node("strong", {}, [em_wrapped]))
            else:
                out.append(_Node("em", {}, _md_to_seminode(n.children, refs, jinja_escape, line)))
        elif n.type == "code_inline":
            is_special = _UNRESOLVED_RE.match(n.content) or _GW_ACTIVE_EXPR_RE.match(n.content)
            if is_special:
                rendered = _render_md_code_inline_or_special(n.content, jinja_escape)
                out.append(_Node("_raw", {}, [rendered]))
            else:
                out.append(_Node("code", {}, [n.content]))
        elif n.type == "link":
            out.append(_Node("_raw", {}, [_render_md_link(n, refs, jinja_escape, line=line)]))
        elif n.type == "image":
            raise ConverterError(
                "unsupported markdown: image not alone in its paragraph/list item "
                f"(an embed must be its own block — see the module docstring) (line {line})"
            )
        elif n.type == "html_inline":
            raise ConverterError(
                f"unsupported markdown: inline HTML ({n.content!r}) (line {line}) "
                r"— escape a literal '<' as '\<' if this is plain text"
            )
        else:
            raise ConverterError(f"unsupported markdown construct: {n.type} (line {line})")
    return out


def _render_seminode_html(
    items: list[_Node | str], refs: RefResolver | None, jinja_escape: bool, line: int
) -> str:
    items = _merge_adjacent_inline(items, None)
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(_render_md_text(item, refs, jinja_escape))
        elif item.tag == "br":
            out.append("<br>")
        elif item.tag == "_raw":
            out.append(str(item.children[0]))
        elif item.tag in ("strong", "em"):
            inner = _render_seminode_html(item.children, refs, jinja_escape, line)
            out.append(f"<{item.tag}>{inner}</{item.tag}>")
        elif item.tag == "code":
            content = "".join(str(c) for c in item.children)
            out.append(_render_md_code_inline_or_special(content, jinja_escape))
        else:
            raise ConverterError(f"unexpected internal node <{item.tag}>")  # pragma: no cover
    return "".join(out)


def _render_inline_nodes(
    nodes: list[SyntaxTreeNode], refs: RefResolver | None, jinja_escape: bool, *, line: int = 0
) -> str:
    seminode = _md_to_seminode(nodes, refs, jinja_escape, line)
    return _render_seminode_html(seminode, refs, jinja_escape, line)


def _render_md_link(
    node: SyntaxTreeNode, refs: RefResolver | None, jinja_escape: bool, *, line: int = 0
) -> str:
    href = str(node.attrs.get("href", "") or "")
    if href.startswith("evidence/"):
        return _push_cross_ref(href, refs)
    title = node.attrs.get("title")
    title_attr = f' title="{_esc(str(title))}"' if title else ""
    text = _render_inline_nodes(node.children, refs, jinja_escape, line=line)
    return f'<a href="{_esc(href)}"{title_attr} target="_blank" rel="noopener">{text}</a>'


def _render_md_code_inline_or_special(content: str, jinja_escape: bool) -> str:
    """A ``code_inline`` token's content: the reserved ``gw:`` forms (an
    unresolved-reference marker or an active template expression — see module
    docstring) bypass normal ``<code>`` rendering entirely; anything else renders
    as a real, D10-escaped-if-requested code span."""
    m = _UNRESOLVED_RE.match(content)
    if m:
        return _render_unresolved_marker(m, position="inline")
    m2 = _GW_ACTIVE_EXPR_RE.match(content)
    if m2:
        return m2.group("expr")  # emitted un-escaped, verbatim: stays an active expression
    text = _esc(content)
    if jinja_escape:
        text = _jinja_escape_html(text)
    return f"<code>{text}</code>"


def _render_md_text(text: str, refs: RefResolver | None, jinja_escape: bool) -> str:
    """Render a plain inline-text token: the reserved ``gw:`` prefix (active
    template expression / unresolved reference) is recognized as the WHOLE
    token's content only when it exactly matches one of those forms (produced
    only inside a ``code_inline`` token, handled by
    :func:`_render_md_code_inline_or_special` instead) — a bare text node never
    carries it. Ordinary text is HTML-escaped, then D10-escaped if requested."""
    del refs
    escaped = _esc(text)
    if jinja_escape:
        escaped = _jinja_escape_html(escaped)
    return escaped
