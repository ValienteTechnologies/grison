"""markdown->html: :func:`md_to_html` itself, plus block-level parsing/
validation (paragraphs, ATX headings, ``bullet_list``/``ordered_list``/list
items, fenced code blocks, blockquotes, GFM tables) against grison's own
renderer — never markdown-it's HTML renderer — so the output stays
byte-for-byte in Ghostwriter's canonical shape.

The WHOLE document is parsed once with markdown-it-py's real CommonMark block
parser (the ``table`` rule re-enabled — see ``mdparse.py`` — everything else
left at the ``commonmark`` preset's defaults) into a SyntaxTreeNode tree,
which is walked and validated against grison's allow-list — every block/
inline node type not on it raises ConverterError naming the construct and its
source line (from the token's ``.map``) — then rendered with grison's OWN
renderer.
"""

from __future__ import annotations

from markdown_it.tree import SyntaxTreeNode

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.grammar import _MAX_NESTED_LIST_DEPTH, _UNRESOLVED_RE
from grison.markdown.converter.mdparse import _MD
from grison.markdown.converter.to_html.common import _inline_children, _node_line
from grison.markdown.converter.to_html.evidence import _push_embed_ref, _render_unresolved_marker
from grison.markdown.converter.to_html.fence import _render_fence_node
from grison.markdown.converter.to_html.inline import _render_inline_nodes
from grison.markdown.converter.to_html.table import _render_table_node
from grison.markdown.refs import RefResolver

_ATX_MARKUP = frozenset({"#", "##", "###", "####", "#####", "######"})


def md_to_html(
    md: str, *, headings: bool = False, refs: RefResolver | None = None, jinja_escape: bool = True
) -> str:
    """Convert markdown (the tiny closed GW subset, plus the reference/template
    special forms) to an HTML fragment. ``headings=True`` also accepts ATX
    headings ``# ``-``###### `` (report-narrative fields and finding fields alike —
    see :func:`html_to_md`). ``refs``, if given,
    resolves embed/cross-reference markdown into Ghostwriter's native HTML forms
    (see module docstring); without one, any of them raises ``ConverterError``.
    ``jinja_escape=True`` (the default, for Ghostwriter-bound text) wraps literal
    template delimiters in author text so Ghostwriter's Jinja compiler renders
    them back literally (D10) — see module docstring for the escape form and the
    reserved ``gw:`` inline-code forms that bypass it.
    """
    if not md.strip():
        return ""
    normalized = md.replace("\r\n", "\n")
    env: dict = {}
    tokens = _MD.parse(normalized, env)
    if env.get("references"):
        # A link reference definition ("[foo]: url") is consumed by markdown-it's
        # "reference" block rule with NO token emitted at all — silently dropping
        # it would violate "hard rejection, never silent"; catch it here since
        # there's no tree node to reject it from.
        name = next(iter(env["references"]))
        raise ConverterError(f"unsupported markdown: link reference definition ({name!r})")
    tree = SyntaxTreeNode(tokens)
    blocks = [
        _render_block_node(
            child, headings=headings, refs=refs, jinja_escape=jinja_escape, nesting=0
        )
        for child in tree.children
    ]
    return "\n\n".join(b for b in blocks if b)


def _render_block_node(
    node: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    nesting: int,
) -> str:
    if node.type == "paragraph":
        return _render_paragraph_node(node, refs=refs, jinja_escape=jinja_escape)
    if node.type == "heading":
        if not headings:
            raise ConverterError(f"unsupported markdown: ATX heading (line {_node_line(node)})")
        if node.markup not in _ATX_MARKUP:
            raise ConverterError(
                f"unsupported markdown: setext heading underline (line {_node_line(node)})"
            )
        level = node.tag[1]
        inner = _render_inline_nodes(
            _inline_children(node), refs, jinja_escape, line=_node_line(node)
        )
        return f"<h{level}>{inner}</h{level}>"
    if node.type in ("bullet_list", "ordered_list"):
        return _render_md_list_node(
            node, headings=headings, refs=refs, jinja_escape=jinja_escape, nesting=nesting
        )
    if node.type == "fence":
        return _render_fence_node(node, jinja_escape=jinja_escape)
    if node.type == "blockquote":
        return _render_blockquote_node(
            node, headings=headings, refs=refs, jinja_escape=jinja_escape
        )
    if node.type == "table":
        return _render_table_node(node, refs=refs, jinja_escape=jinja_escape)
    name = _BLOCK_TYPE_NAMES.get(node.type, node.type)
    raise ConverterError(f"unsupported markdown: {name} (line {_node_line(node)})")


_BLOCK_TYPE_NAMES = {
    "hr": "thematic break",
    "code_block": "indented code block",
    "html_block": "raw HTML block",
}


def _render_paragraph_node(
    node: SyntaxTreeNode, *, refs: RefResolver | None, jinja_escape: bool
) -> str:
    """A paragraph whose ONLY inline content is a single image is the embed form
    (D1/D9); one whose only content is a reserved unresolved-reference marker
    (see module docstring) reconstructs that construct directly — both are their
    own block (top-level, or a direct child block of a list item), never wrapped
    in ``<p>``. Every other paragraph — including every list-item paragraph —
    renders wrapped in ``<p>``: Ghostwriter's real TipTap list-item schema always
    contains at least one paragraph block (a ProseMirror ``listItem`` node's
    content is block-only — bare inline ``<li>`` text is never actually stored),
    so grison's canonical push output matches that unconditionally rather than
    reproducing CommonMark's own tight/loose distinction, which Ghostwriter's
    editor has no concept of at all."""
    inline_children = _inline_children(node)
    if len(inline_children) == 1:
        only = inline_children[0]
        if only.type == "image":
            return _push_embed_ref(str(only.attrs.get("src", "") or ""), refs)
        if only.type == "code_inline":
            m = _UNRESOLVED_RE.match(only.content)
            if m:
                return _render_unresolved_marker(m, position="block")
    line = _node_line(node)
    return f"<p>{_render_inline_nodes(inline_children, refs, jinja_escape, line=line)}</p>"


def _render_blockquote_node(
    node: SyntaxTreeNode, *, headings: bool, refs: RefResolver | None, jinja_escape: bool
) -> str:
    """Render a ``blockquote`` node's block children — paragraphs and lists only
    (module docstring, "Special forms"/blockquote): a fence, table, heading, or
    nested blockquote inside one is outside this vocabulary and raises the same
    "unsupported markdown: <type> inside a blockquote" ``ConverterError`` any
    other disallowed list-item child does, rather than silently degrading or
    (for a nested blockquote) collapsing levels the way lists do — GW's real
    ``Blockquote`` node (StarterKit defaults) nests directly under a top-level
    field with no analogous "one level, then collapse" precedent to reuse."""
    children_html = []
    for child in node.children:
        if child.type == "paragraph":
            children_html.append(
                _render_paragraph_node(child, refs=refs, jinja_escape=jinja_escape)
            )
        elif child.type in ("bullet_list", "ordered_list"):
            children_html.append(
                _render_md_list_node(
                    child, headings=headings, refs=refs, jinja_escape=jinja_escape, nesting=0
                )
            )
        else:
            raise ConverterError(
                f"unsupported markdown: {child.type} inside a blockquote (line {_node_line(child)})"
            )
    return f"<blockquote>{''.join(children_html)}</blockquote>"


def _render_md_list_node(
    node: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    nesting: int,
) -> str:
    """Render a ``bullet_list``/``ordered_list`` node. ``_MAX_NESTED_LIST_DEPTH``
    (1) level of nesting is supported: an item's OWN nested list renders inside
    its ``<li>`` (``nesting`` goes from 0 at the top level to 1 there); a list
    nested inside THAT one (``nesting`` would reach ``_MAX_NESTED_LIST_DEPTH + 1``)
    is a hard ``ConverterError`` naming the line — the HTML->markdown side still
    collapses a real GW record's 3rd+ level into that same one level (with
    ``on_loss``, see ``_flatten_nested_list`` in ``from_html/blocks.py``) since
    existing corpus data has it, but freshly-authored markdown must not grow a
    level nothing can round-trip."""
    is_ol = node.type == "ordered_list"
    tag = "ol" if is_ol else "ul"
    start = node.attrs.get("start")
    attr = f' start="{start}"' if is_ol and start not in (None, 1) else ""
    items = [
        _render_list_item_node(
            li, headings=headings, refs=refs, jinja_escape=jinja_escape, nesting=nesting
        )
        for li in node.children
    ]
    return f"<{tag}{attr}>{''.join(items)}</{tag}>"


def _render_list_item_node(
    li: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    nesting: int,
) -> str:
    """An item's own block children: a nested list renders inside its ``<li>``
    (subject to the one-level nesting limit); a paragraph renders ``<p>``-
    wrapped (or as its own embed/marker block — see ``_render_paragraph_node``);
    a fence renders as its own ``<pre>`` block — the "step text, then a fence"
    replication-steps pattern real report authors hit constantly (module
    docstring, "Special forms"/fence) — simply appended after the paragraph's
    HTML, exactly like the existing paragraph-then-embed multi-block shape:
    Ghostwriter's real ``ListItem`` schema (``content: 'paragraph block*'``)
    already allows any further block after the first, so no special "loose
    item" HTML shape is needed here (that distinction only matters on the
    HTML->markdown side, rendering back to CommonMark's own loose-list-item
    indentation — see ``_render_li`` in ``from_html/blocks.py``)."""
    blocks_html: list[str] = []
    nested_html = ""
    for child in li.children:
        if child.type in ("bullet_list", "ordered_list"):
            if nesting >= _MAX_NESTED_LIST_DEPTH:
                raise ConverterError(
                    "unsupported markdown: list nested more than one level deep "
                    f"(line {_node_line(child)})"
                )
            nested_html += _render_md_list_node(
                child, headings=headings, refs=refs, jinja_escape=jinja_escape, nesting=nesting + 1
            )
        elif child.type == "paragraph":
            blocks_html.append(_render_paragraph_node(child, refs=refs, jinja_escape=jinja_escape))
        elif child.type == "fence":
            blocks_html.append(_render_fence_node(child, jinja_escape=jinja_escape))
        else:
            raise ConverterError(
                f"unsupported markdown: {child.type} inside a list item (line {_node_line(child)})"
            )
    return f"<li>{''.join(blocks_html)}{nested_html}</li>"
