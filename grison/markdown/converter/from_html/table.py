"""HTML->markdown: GFM pipe tables (module docstring, "Special forms"/table).
Accepts every real Ghostwriter/legacy-scanner shape variant: with or without
``<thead>``/``<tbody>``, ``<th>`` or a bare first-row ``<td>`` for the header
(the FIRST row is always treated as the header, whichever tag its cells use —
GFM itself requires a header row; there is no markdown shape for "no header"
to fall back to), cells with or without a wrapping ``<p>``, and the
``collab-table-wrapper`` div TipTap's ``TableWithCaption`` extension wraps a
real table in (its caption paragraph renders as a plain paragraph AFTER the
table — module docstring: the caption node itself is never round-tripped as a
caption, only its text). Column alignment (a ``style="text-align:..."``
attribute on a header cell) has no GFM representation and is dropped like any
other unrecognized attribute (the generic on_loss path — see ``grammar.py``'s
``_KEEP_ATTRS``, which keeps nothing at all for ``<table>``/``<thead>``/
``<tbody>``/``<tr>``/``<th>``/``<td>``, so colspan/rowspan/style/data-bg-color/
colwidth are ALL reported this same way); a cell's colspan/rowspan is silently
flattened to an ordinary single cell holding just its own content — GFM has no
way to make a cell visually span more than one row/column, and a slightly
ragged resulting table (GFM tolerates a row with fewer/more cells than the
header) is the closest a plain pipe table can get.
"""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.from_html.inline import _render_inline
from grison.markdown.converter.grammar import _HEADING_TAGS
from grison.markdown.converter.jinja import _RawScanState
from grison.markdown.converter.mdtext import _finalize_line
from grison.markdown.converter.nodes import _Node, _report_dropped_attrs, _report_loss
from grison.markdown.refs import RefResolver

_TABLE_SECTION_TAGS = ("thead", "tbody", "tfoot")
_CELL_BLOCK_TAGS = {"p", "ul", "ol", "table", "div"} | _HEADING_TAGS


def _collect_trs(node: _Node, on_loss: Callable[[str], None] | None) -> list[_Node]:
    _report_dropped_attrs(node, on_loss)
    trs: list[_Node] = []
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                raise ConverterError("stray text directly inside <table>")
            continue
        if child.tag in _TABLE_SECTION_TAGS:
            _report_dropped_attrs(child, on_loss)
            for tr in child.children:
                if isinstance(tr, str):
                    if tr.strip():
                        raise ConverterError(f"stray text directly inside <{child.tag}>")
                    continue
                if tr.tag != "tr":
                    raise ConverterError(f"unsupported <{child.tag}> child: <{tr.tag}>")
                trs.append(tr)
        elif child.tag == "tr":
            trs.append(child)
        else:
            raise ConverterError(f"unsupported <table> child: <{child.tag}>")
    return trs


def _cell_content_children(cell: _Node, on_loss: Callable[[str], None] | None) -> list[_Node | str]:
    """A cell's inline content, unwrapping a single wrapping ``<p>`` (the
    "cells with or without <p>" variant) — anything else block-shaped inside a
    cell (another block alongside/instead of that one ``<p>``, a nested list,
    heading, or table) has no GFM cell representation at all (module docstring:
    "no nested blocks in cells") and is rejected outright, the same as any
    other unsupported construct."""
    non_ws = [c for c in cell.children if not (isinstance(c, str) and c.strip() == "")]
    if len(non_ws) == 1 and isinstance(non_ws[0], _Node) and non_ws[0].tag == "p":
        p = non_ws[0]
        _report_dropped_attrs(p, on_loss)
        return p.children
    for c in non_ws:
        if isinstance(c, _Node) and c.tag in _CELL_BLOCK_TAGS:
            raise ConverterError(
                f"unsupported <table> cell content: <{c.tag}> (a cell holds inline content "
                "only, optionally wrapped in one <p>)"
            )
    return non_ws


def _render_cell(
    cell: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    content_children = _cell_content_children(cell, on_loss)
    if any(isinstance(c, _Node) and c.tag == "br" for c in content_children):
        raise ConverterError(
            "unsupported <br> inside a <table> cell (a GFM table cell can't contain a line break)"
        )
    text = _finalize_line(_render_inline(content_children, refs, on_loss, raw_state), on_loss)
    return text.replace("|", "\\|")


def _render_row(
    tr: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> list[str]:
    _report_dropped_attrs(tr, on_loss)
    cells = []
    for c in tr.children:
        if isinstance(c, str):
            if c.strip():
                raise ConverterError("stray text directly inside <tr>")
            continue
        if c.tag not in ("td", "th"):
            raise ConverterError(f"unsupported <tr> child: <{c.tag}>")
        _report_dropped_attrs(c, on_loss)
        cells.append(_render_cell(c, refs, on_loss, raw_state))
    return cells


def _render_table(
    node: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    trs = _collect_trs(node, on_loss)
    if not trs:
        raise ConverterError("<table> has no rows to become a markdown table")
    rows = [_render_row(tr, refs, on_loss, raw_state) for tr in trs]
    ncols = len(rows[0])
    header_line = "| " + " | ".join(rows[0]) + " |"
    sep_line = "| " + " | ".join(["---"] * ncols) + " |"
    body_lines = ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join([header_line, sep_line, *body_lines])


def _render_table_wrapper(
    node: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    """A ``<div class="collab-table-wrapper">`` — TipTap's ``TableWithCaption``
    shape — wraps exactly one ``<table>`` plus, optionally, one caption
    paragraph (``<p class="collab-table-caption">``, itself wrapping a
    ``<span class="collab-table-caption-content">``, which unwraps like any
    other plain styling span). The caption is never round-tripped AS a
    caption — GFM has no caption construct — so it renders as a plain
    paragraph directly after the table instead, reported via ``on_loss``."""
    _report_dropped_attrs(node, on_loss)
    table_node: _Node | None = None
    caption_text: str | None = None
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                raise ConverterError(
                    'stray text directly inside <div class="collab-table-wrapper">'
                )
            continue
        if child.tag == "table":
            if table_node is not None:
                raise ConverterError(
                    'unsupported <div class="collab-table-wrapper">: more than one <table>'
                )
            table_node = child
        elif child.tag == "p":
            _report_loss(
                on_loss,
                "table caption dropped as a caption (rendered as a plain paragraph after "
                "the table; not round-tripped as a caption)",
            )
            _report_dropped_attrs(child, on_loss)
            caption_text = _finalize_line(
                _render_inline(child.children, refs, on_loss, raw_state), on_loss
            )
        else:
            raise ConverterError(
                f'unsupported <div class="collab-table-wrapper"> child: <{child.tag}>'
            )
    if table_node is None:
        raise ConverterError('<div class="collab-table-wrapper"> has no <table>')
    table_md = _render_table(table_node, refs, on_loss, raw_state)
    return f"{table_md}\n\n{caption_text}" if caption_text else table_md
