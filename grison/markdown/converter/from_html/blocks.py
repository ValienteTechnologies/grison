"""HTML->markdown: :func:`html_to_md` itself, plus top-level block grouping/
merging and block/list rendering (paragraphs, headings, ``<ul>``/``<ol>``/
``<li>``, blockquotes, and the evidence/table-wrapper ``<div>``). Fenced code
blocks (``<pre>``) and GFM tables (``<table>``) each get their own leaf module
(``fence``/``table``)."""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.from_html.evidence import _render_evidence_div, _try_render_dot_embed
from grison.markdown.converter.from_html.fence import _render_pre_node
from grison.markdown.converter.from_html.inline import _render_inline
from grison.markdown.converter.from_html.table import _render_table, _render_table_wrapper
from grison.markdown.converter.grammar import _BLOCK_TAGS, _HEADING_TAGS, _MAX_NESTED_LIST_DEPTH
from grison.markdown.converter.jinja import _note_block_boundary, _RawScanState
from grison.markdown.converter.mdtext import _finalize_line
from grison.markdown.converter.nodes import _Node, _report_dropped_attrs, _report_loss, _TreeBuilder
from grison.markdown.refs import RefResolver


def html_to_md(
    html: str,
    *,
    headings: bool = False,
    refs: RefResolver | None = None,
    on_loss: Callable[[str], None] | None = None,
) -> str:
    """Convert a GW rich-text HTML fragment to markdown. ``headings=True`` also
    accepts ``<h1>``-``<h6>`` — grison's finding adapters pass this too (a real
    TipTap editor emits headings in finding fields as well, not just narrative).

    ``refs``, if given, resolves embedded-evidence/cross-reference constructs (see
    the module docstring); without one, encountering any of them raises
    ``ConverterError``. ``on_loss``, if given, is called once per dropped/
    canonicalized construct (styling spans, non-canonical link rel/target, dropped
    attributes, unresolved references) with a human-readable message. It never
    changes the output — the drop still happens — it only makes the drop visible
    to the caller instead of silent.
    """
    if not html.strip():
        return ""
    builder = _TreeBuilder(headings=headings)
    builder.feed(html)
    builder.close()
    if len(builder.stack) != 1:
        raise ConverterError(f"unclosed HTML tag: <{builder.stack[-1].tag}>")
    blocks = _merge_adjacent_top_level_lists(_group_top_level(builder.root.children), on_loss)
    raw_state = _RawScanState()
    return "\n\n".join(_render_top_level_blocks(blocks, refs, on_loss, raw_state))


def _render_top_level_blocks(
    blocks: list[_Node],
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> list[str]:
    """Render each top-level block, DROPPING a ``<p>``/heading that renders to
    nothing (an empty ``<p></p>``, or one whose only content is CommonMark-
    insignificant whitespace — see ``_finalize_line``) instead of keeping it as an
    empty string in the join. A real GW export (``&nbsp;``-padded or genuinely
    empty trailing paragraphs — TipTap's own editor leaves these behind routinely)
    otherwise leaves a PHANTOM multi-blank-line gap in the output: harmless to a
    human reader, but not to the next round trip — CommonMark itself collapses any
    run of blank lines between real blocks down to a single block boundary on
    every reparse, so ``canonical(md_to_html(html_to_md(h)))`` would drift from
    ``canonical(h)`` (the exact bug behind a fresh pull's spurious one-time settle
    push) unless grison's OWN output already reflects that collapse up front. A
    ``<ul>``/``<ol>`` is never dropped this way — an empty list is already
    vanishingly unlikely real data and has no analogous "collapses to nothing"
    CommonMark behavior to match."""
    rendered: list[str] = []
    for block in blocks:
        text = _render_block(block, refs, on_loss, raw_state)
        _note_block_boundary(raw_state, on_loss)
        if text == "" and block.tag != "ul" and block.tag != "ol":
            _report_loss(
                on_loss,
                f"empty/whitespace-only <{block.tag}> block dropped (no markdown representation)",
            )
            continue
        rendered.append(text)
    return rendered


_TOP_LEVEL_BLOCK_TAGS = ("p", "ul", "ol", "div", "pre", "blockquote", "table")


def _group_top_level(children: list[_Node | str]) -> list[_Node]:
    """Split top-level children into p/ul/ol/div/pre/blockquote/table/heading
    blocks, wrapping stray inline content in an implicit paragraph and dropping
    insignificant top-level whitespace."""
    blocks: list[_Node] = []
    buffer: list[_Node | str] = []

    def flush() -> None:
        if buffer:
            blocks.append(_Node("p", children=list(buffer)))
            buffer.clear()

    for child in children:
        if isinstance(child, str) and child.strip() == "":
            continue
        if isinstance(child, _Node) and (
            child.tag in _TOP_LEVEL_BLOCK_TAGS or child.tag in _HEADING_TAGS
        ):
            flush()
            blocks.append(child)
        else:
            buffer.append(child)
    flush()
    return blocks


def _merge_adjacent_top_level_lists(
    blocks: list[_Node], on_loss: Callable[[str], None] | None
) -> list[_Node]:
    """Two adjacent top-level ``<ul>``/``<ol>`` blocks of the SAME tag merge
    into one — visually identical (a browser renders two immediately-adjacent
    same-type lists exactly like one), and necessary: rendered as two separate
    markdown list blocks separated by a blank line, real CommonMark reads that
    BACK as one loose list (a blank line between two lists of the same marker
    type doesn't start a new list — see the module docstring's list section),
    so grison's OWN un-merged output would silently retighten on the very next
    push/pull round. Reported via ``on_loss`` as a normalization."""
    merged: list[_Node] = []
    for block in blocks:
        if merged and block.tag in ("ul", "ol") and merged[-1].tag == block.tag:
            prev = merged[-1]
            merged[-1] = _Node(
                prev.tag, dict(prev.attrs), list(prev.children) + list(block.children)
            )
            _report_loss(on_loss, f"adjacent <{block.tag}> lists merged into one (normalization)")
            continue
        merged.append(block)
    return merged


def _is_bare_text(children: list[_Node | str]) -> str | None:
    """If ``children`` is exactly one text node (ignoring nothing else at all),
    return its content; else ``None``."""
    if len(children) == 1 and isinstance(children[0], str):
        return children[0]
    return None


def _render_block(
    node: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    if node.tag == "p":
        text = _is_bare_text(node.children)
        if text is not None:
            embed = _try_render_dot_embed(text, refs, on_loss)
            if embed is not None:
                return embed
        _report_dropped_attrs(node, on_loss)
        return _finalize_line(_render_inline(node.children, refs, on_loss, raw_state), on_loss)
    if node.tag in _HEADING_TAGS:
        _report_dropped_attrs(node, on_loss)
        return (
            "#" * int(node.tag[1]) + " " + _render_inline(node.children, refs, on_loss, raw_state)
        )
    if node.tag in ("ul", "ol"):
        return _render_list_node(node, refs, on_loss, raw_state)
    if node.tag == "pre":
        return _render_pre_node(node, on_loss, raw_state)
    if node.tag == "blockquote":
        return _render_blockquote(node, refs, on_loss, raw_state)
    if node.tag == "table":
        return _render_table(node, refs, on_loss, raw_state)
    if node.tag == "div":
        classes = (node.attrs.get("class") or "").split()
        if "collab-table-wrapper" in classes:
            return _render_table_wrapper(node, refs, on_loss, raw_state)
        return _render_evidence_div(node, refs, on_loss)
    raise ConverterError(f"unsupported block-level tag: <{node.tag}>")


def _render_blockquote(
    node: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    """Render a ``<blockquote>``'s block children — paragraphs and lists only
    (module docstring, "Special forms"/blockquote) — to ``> ``-prefixed lines,
    a blank line inside the quote rendering as a bare ``>`` (CommonMark's own
    lazy-continuation-free blockquote-marker convention: every physical line
    that belongs to the quote, blank or not, carries the marker). A nested
    blockquote, or anything else GW's real ``Blockquote`` node (StarterKit
    defaults) doesn't hold, is outside this vocabulary and raises the same
    "unsupported" ``ConverterError`` any other disallowed content does."""
    _report_dropped_attrs(node, on_loss)
    rendered_blocks: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                raise ConverterError(
                    "stray text directly inside <blockquote> (expected <p>/<ul>/<ol>)"
                )
            continue
        if child.tag == "p":
            _report_dropped_attrs(child, on_loss)
            text = _finalize_line(_render_inline(child.children, refs, on_loss, raw_state), on_loss)
        elif child.tag in ("ul", "ol"):
            text = _render_list_node(child, refs, on_loss, raw_state)
        else:
            raise ConverterError(
                f"unsupported <{child.tag}> inside <blockquote> (expected <p>/<ul>/<ol>)"
            )
        rendered_blocks.append(text)
    body = "\n\n".join(b for b in rendered_blocks if b != "")
    return "\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))


def _ol_start(node: _Node, on_loss: Callable[[str], None] | None) -> int:
    """Numbering start for an ``<ol>`` — ``start`` if given (else 1). ``type``
    (a/i/A/I numbering) can't be represented in markdown, so it's dropped and
    reported via ``on_loss`` — items are always rendered as decimal ``N. ``."""
    type_attr = node.attrs.get("type")
    if type_attr:
        _report_loss(
            on_loss, f"<ol type={type_attr!r}> numbering style dropped (rendered as 1. 2. 3. ...)"
        )
    start_attr = node.attrs.get("start")
    if start_attr:
        try:
            return int(start_attr)
        except ValueError:
            pass
    return 1


def _render_list_node(
    node: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    is_ol = node.tag == "ol"
    n = _ol_start(node, on_loss) if is_ol else 1
    _report_dropped_attrs(node, on_loss)
    lines = []
    for li in node.children:
        if isinstance(li, str):
            if li.strip() == "":
                continue
            raise ConverterError(f"stray text directly inside <{node.tag}> (expected <li>)")
        if li.tag != "li":
            raise ConverterError(f"unsupported <{node.tag}> child: <{li.tag}>")
        _report_dropped_attrs(li, on_loss)
        item_lines = _render_li(li, refs, on_loss, raw_state)
        marker = f"{n}. " if is_ol else "- "
        lines.append(marker + item_lines[0])
        # CommonMark requires a nested block to be indented to (at least) this
        # item's OWN marker width to actually belong to it — grison's own output
        # must be real, previewable CommonMark (an AI author previewing it in any
        # CommonMark renderer must see the same nesting grison itself will re-read
        # on push), so the indent is marker-width, not a flat convention.
        indent = " " * len(marker)
        lines.extend(indent + m if m else "" for m in item_lines[1:])
        if is_ol:
            n += 1
    return "\n".join(lines)


_LIST_TAGS = ("ul", "ol")
_LI_BLOCK_TAGS = ("p", "div", "pre")


def _render_li(
    li: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
    depth: int = 0,
) -> list[str]:
    """Render one ``<li>``'s content, unwrapping the ``<p>`` GW wraps item content
    in. Returns the item's OWN physical output lines, not yet marker-prefixed —
    the caller adds the ``- ``/``N. `` marker to line 0 and indents the rest to
    match (nested nested sub-items' own markers, or a blank line + indented
    continuation block for a multi-block/loose item — see module docstring).

    A bare inline ``<li>`` (no ``<p>``/``<div>``/``<pre>`` child at all — what
    THIS converter itself emits for a single-block item) and an ``<li>`` with
    exactly one ``<p>`` (with or without a nested list) both render as one bare
    line: the corpus impact/mitigation/references fields are
    ``<ul><li><p>…</p></li></ul>``, so whitespace round-trips exactly rather
    than being paragraph-joined/stripped. Two or more blocks (multiple ``<p>``,
    or a ``<p>``/embed-``<div>``/fence-``<pre>`` mix — the "step text, then a
    fence" replication-steps shape real report authors hit constantly) render
    as a loose item: each extra block becomes a blank line then an indented
    continuation. A fence's own rendered text is itself multi-line (opening
    marker, content, closing marker) and — unlike a plain paragraph's own
    internal hard-break continuation lines, which CommonMark's lazy
    continuation lets stay unindented (see
    ``test_lazy_continuation_line_joins_list_item_paragraph``) — a fenced code
    block gets NO such laziness: every one of its physical lines needs the
    item's own marker-width indent to still parse as belonging to it, so its
    lines are appended to the returned list SEPARATELY (never combined into one
    multi-line string element) so the caller's existing per-element indent
    (``_render_list_node``) reaches every one of them.

    A ``<table>``/``<blockquote>`` (or any other block-level tag, or a heading
    when ``headings=True``) sitting alongside/instead of the recognized
    ``<p>``/``<div>``/``<pre>``/``<ul>``/``<ol>`` children is OUTSIDE this
    vocabulary — matching ``to_html/blocks.py``'s ``_render_list_item_node``,
    which already hard-rejects the same shapes on the markdown-authoring side
    — and raises the same "unsupported markdown: ... inside a list item"
    ``ConverterError`` rather than being silently dropped. Bug fix (lab
    finding, converter-grammar-lab, 2026-09-21): the OLD code only ever
    collected recognized siblings into ``blocks`` below and never inspected
    what it left out, so an unrecognized sibling next to a single supported
    ``<p>`` (e.g. ``<li><p>text</p><table>...</table></li>``) hit the
    single-``<p>``-item fast path a few lines down and the ``<table>`` was
    dropped on the floor with no error and no ``on_loss`` report at all.
    """
    for child in li.children:
        if (
            isinstance(child, _Node)
            and (child.tag in _BLOCK_TAGS or child.tag in _HEADING_TAGS)
            and child.tag not in _LI_BLOCK_TAGS
            and child.tag not in _LIST_TAGS
        ):
            raise ConverterError(f"unsupported markdown: {child.tag} inside a list item")
    blocks = [c for c in li.children if isinstance(c, _Node) and c.tag in _LI_BLOCK_TAGS]
    lists = [c for c in li.children if isinstance(c, _Node) and c.tag in _LIST_TAGS]
    nested: list[str] = []
    for lst in lists:
        _report_dropped_attrs(lst, on_loss)
        nested.extend(_flatten_nested_list(lst, refs, on_loss, raw_state, depth + 1))

    if not blocks:
        inline_children = [
            c for c in li.children if not (isinstance(c, _Node) and c.tag in _LIST_TAGS)
        ]
        head = _finalize_line(_render_inline(inline_children, refs, on_loss, raw_state), on_loss)
        return [head, *nested]
    if len(blocks) == 1 and blocks[0].tag == "p":
        _report_dropped_attrs(blocks[0], on_loss)
        head = _finalize_line(_render_inline(blocks[0].children, refs, on_loss, raw_state), on_loss)
        return [head, *nested]

    lines: list[str] = []
    for b in blocks:
        if b.tag == "div":
            _report_dropped_attrs(b, on_loss)
            block_lines = [_render_evidence_div(b, refs, on_loss)]
        elif b.tag == "pre":
            # _render_pre_node reports its own dropped attrs internally.
            block_lines = _render_pre_node(b, on_loss, raw_state).split("\n")
        else:
            _report_dropped_attrs(b, on_loss)
            bare_text = _is_bare_text(b.children)
            embed = (
                _try_render_dot_embed(bare_text, refs, on_loss) if bare_text is not None else None
            )
            text = (
                embed
                if embed is not None
                else _finalize_line(_render_inline(b.children, refs, on_loss, raw_state), on_loss)
            )
            block_lines = [text]
        if lines:
            lines.append("")
        lines.extend(block_lines)
    lines.extend(nested)
    return lines


def _flatten_nested_list(
    lst: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
    depth: int = 1,
) -> list[str]:
    """Flatten a ``<ul>``/``<ol>`` nested inside an ``<li>`` — and anything nested
    inside IT — into a flat list of marker-prefixed sub-item lines, all at the one
    supported nesting level (the indent, matching the OUTER item's own marker
    width, is applied by the caller). ``depth`` counts nesting levels from 1 (see
    ``_MAX_NESTED_LIST_DEPTH``); a level deeper than that is a 3rd-or-deeper level
    already present in the source HTML being collapsed into that same one level —
    reported via ``on_loss`` since it's a real, if rare, loss of structure (the
    markdown side hard-rejects an attempt to author one instead — see
    ``_render_list_item_node`` in ``to_html/blocks.py``)."""
    if depth > _MAX_NESTED_LIST_DEPTH:
        _report_loss(
            on_loss,
            f"<{lst.tag}> nested {depth + 1} levels deep collapsed into the one supported "
            "sub-level",
        )
    is_ol = lst.tag == "ol"
    n = _ol_start(lst, on_loss) if is_ol else 1
    lines: list[str] = []
    for li in lst.children:
        if isinstance(li, str):
            if li.strip() == "":
                continue
            raise ConverterError(f"stray text directly inside <{lst.tag}> (expected <li>)")
        if li.tag != "li":
            raise ConverterError(f"unsupported <{lst.tag}> child: <{li.tag}>")
        _report_dropped_attrs(li, on_loss)
        item_lines = _render_li(li, refs, on_loss, raw_state, depth)
        if item_lines and item_lines[0]:
            marker = f"{n}. " if is_ol else "- "
            lines.append(marker + item_lines[0])
            # A 3rd+ level's own lines are already fully formed (marker-prefixed)
            # by a deeper call to this same function — added verbatim, NOT
            # re-indented, so indent is applied exactly once (by the outermost
            # `_render_list_node`), collapsing every deeper level into this one.
            lines.extend(item_lines[1:])
            if is_ol:
                n += 1
        else:
            lines.extend(item_lines[1:])
    return lines
