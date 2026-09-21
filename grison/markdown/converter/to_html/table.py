"""markdown->html: GFM pipe tables (module docstring, "Special forms"/table) —
the canonical push shape is
``<table><tbody><tr><th><p>…</p></th>…</tr><tr><td><p>…</p></td>…</tr>…</tbody></table>``
(matching plain ``@tiptap/extension-table``'s Table/TableRow/TableHeader/
GwTableCell shape — no ``<thead>``; header cells are just ``<th>`` sitting in
the same ``<tbody>`` as every other row). Column alignment (a GFM
``:---:``/``:--``/``--:`` separator) has no representation in this shape and
is dropped outright — never emitted as a style attribute or anything else —
Ghostwriter's real table editor has no per-column alignment concept to
receive it anyway; a table with no header row at all isn't valid GFM to begin
with, so markdown-it's own table rule never hands one to this renderer.
"""

from __future__ import annotations

from markdown_it.tree import SyntaxTreeNode

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.to_html.common import _inline_children, _node_line
from grison.markdown.converter.to_html.inline import _render_inline_nodes
from grison.markdown.refs import RefResolver


def _render_table_node(
    node: SyntaxTreeNode, *, refs: RefResolver | None, jinja_escape: bool
) -> str:
    rows = _table_rows(node)
    if not rows:
        raise ConverterError(f"unsupported markdown: empty table (line {_node_line(node)})")

    def render_row(cells: list[SyntaxTreeNode], tag: str) -> str:
        tds = []
        for cell in cells:
            inline = cell.children[0] if cell.children else None
            line = (inline.map[0] + 1) if inline is not None and inline.map else _node_line(node)
            content = _render_inline_nodes(_inline_children(cell), refs, jinja_escape, line=line)
            tds.append(f"<{tag}><p>{content}</p></{tag}>")
        return f"<tr>{''.join(tds)}</tr>"

    header_html = render_row(rows[0], "th")
    body_html = "".join(render_row(r, "td") for r in rows[1:])
    return f"<table><tbody>{header_html}{body_html}</tbody></table>"


def _table_rows(node: SyntaxTreeNode) -> list[list[SyntaxTreeNode]]:
    """Every ``tr``'s cells, in document order. markdown-it's own table rule
    always groups rows under a ``thead``/``tbody`` container node (never
    bare ``tr`` children directly on ``table``) — the FIRST row (always inside
    ``thead``) is the header; ``tbody`` is entirely absent for a header-only
    table with no data rows."""
    rows: list[list[SyntaxTreeNode]] = []
    for section in node.children:
        for tr in section.children:
            rows.append(list(tr.children))
    return rows
