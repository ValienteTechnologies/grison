"""Bespoke HTML<->markdown converter for the tiny closed vocabulary Ghostwriter's
rich-text fields accept.

Ghostwriter finding fields render a small, fixed subset of HTML (paragraphs,
unordered lists, bold/code/em/links/hard-breaks, plus TinyMCE's cosmetic
``<span>`` highlight wrapper). grison round-trips those fields against local
markdown, so this module hand-rolls both directions instead of depending on a
general-purpose HTML/markdown library: anything outside the whitelist below
must fail loudly (:class:`ConverterError`) rather than degrade silently or
get dropped on the floor.

Whitelist (both directions):
  block:  ``<p>`` <-> paragraph, ``<ul><li>`` <-> ``- `` list item,
          ``<ol><li>`` <-> ``1. `` list item (numbered sequentially on emit;
          ``<ol start="N">`` <-> the first item's literal number)
  inline: ``<strong>`` <-> ``**bold**``/``__bold__``, ``<code>`` <-> `` `code` ``,
          ``<em>`` <-> ``*em*``/``_em_``, ``<strong><em>`` <-> ``***both***``,
          ``<a href[ title]>`` <-> ``[text](url[ "title"])``,
          ``<br>`` <-> a hard line break inside a paragraph
  ``<span>`` is unwrapped (kept, tag dropped) rather than rejected, since
  TinyMCE wraps highlighted text in it. An ``<ol type="...">`` (a/i/A/I
  numbering style) is dropped the same way — markdown can't represent it —
  and reported via ``on_loss``.

Nesting: inline tokens nest arbitrarily inside bold/em/link text in both
directions (e.g. a link whose visible text contains ``<strong>``). Lists
support one level of nesting: a ``<ul>``/``<ol>`` nested inside an ``<li>``
renders as a 2-space-indented sub-item (``  - `` or ``  N. `` per its own
tag); a list nested inside ONE OF THOSE (three or more levels deep in the
source) collapses into that same single sub-level rather than growing a
third indent — markdown here only has one nesting convention. ``<ul>`` and
``<ol>`` mix freely at any level (an ordered list nested in a bullet item's
``<li>``, or vice versa).

Loss visibility: constructs GW's HTML carries but this vocabulary can't
represent — TinyMCE ``data-color``/``style`` highlight spans, non-canonical
link ``rel``/``target`` values — are still dropped/canonicalized exactly as
before, but ``html_to_md`` accepts an optional ``on_loss`` callback invoked
with a human-readable message per dropped construct, so callers can surface
the loss instead of it being silent.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser


class ConverterError(ValueError):
    """Raised when HTML or markdown outside the tiny closed GW vocabulary is seen."""


_BLOCK_TAGS = {"p", "ul", "ol", "li"}
_INLINE_TAGS = {"strong", "code", "em", "a", "br"}
_UNWRAP_TAGS = {"span"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_ALLOWED_TAGS = _BLOCK_TAGS | _INLINE_TAGS | _UNWRAP_TAGS
# Report-narrative fields (report.extraFields) use the same vocabulary as finding
# fields plus headings — the finding converter rejects headings as a corruption
# tripwire, so heading support is opt-in via ``headings=True`` and never loosens the
# strict finding path.


def _report_loss(on_loss: Callable[[str], None] | None, msg: str) -> None:
    if on_loss:
        on_loss(msg)


def _esc(text: str) -> str:
    """HTML-escape text/attribute content. ``"`` is escaped too so a URL containing a
    quote can't break out of the ``href="…"`` attribute and inject markup."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --- html -> markdown -------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Builds a tiny tree from an HTML fragment, rejecting non-whitelisted tags."""

    def __init__(self, *, headings: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack: list[_Node] = [self.root]
        self._allowed = _ALLOWED_TAGS | _HEADING_TAGS if headings else _ALLOWED_TAGS

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)  # self-closing form, e.g. <br/>

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._allowed:
            raise ConverterError(f"unsupported HTML tag: <{tag}>")
        if tag == "br":
            self.stack[-1].children.append(_Node("br"))
            return
        node_attrs: dict[str, str] = {}
        if tag == "a":
            for name, value in attrs:
                # href/title are load-bearing; rel/target are captured only so the
                # render step can warn when they diverge from the canonical values
                # grison substitutes on push — not otherwise preserved.
                if name in ("href", "rel", "target", "title"):
                    node_attrs[name] = value or ""
        elif tag == "span":
            # Captured only for on_loss reporting (F4) — the span is still unwrapped.
            for name, value in attrs:
                if name in ("data-color", "style"):
                    node_attrs[name] = value or ""
        elif tag == "ol":
            # start is load-bearing (sets the rendered numbering); type is captured
            # only for on_loss reporting — markdown can't represent a/i/A/I numbering.
            for name, value in attrs:
                if name in ("start", "type"):
                    node_attrs[name] = value or ""
        node = _Node(tag, node_attrs)
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag == "br":
            return
        if tag not in self._allowed:
            raise ConverterError(f"unsupported HTML tag: </{tag}>")
        if len(self.stack) <= 1 or self.stack[-1].tag != tag:
            raise ConverterError(f"mismatched closing tag: </{tag}>")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def html_to_md(
    html: str,
    *,
    headings: bool = False,
    on_loss: Callable[[str], None] | None = None,
) -> str:
    """Convert a GW rich-text HTML fragment to markdown. ``headings=True`` also
    accepts ``<h1>``–``<h6>`` (for report-narrative fields, not finding fields).

    ``on_loss``, if given, is called once per dropped/canonicalized construct
    (styling spans, non-canonical link rel/target) with a human-readable message.
    It never changes the output — the drop still happens — it only makes the drop
    visible to the caller instead of silent.
    """
    builder = _TreeBuilder(headings=headings)
    builder.feed(html)
    builder.close()
    if len(builder.stack) != 1:
        raise ConverterError(f"unclosed HTML tag: <{builder.stack[-1].tag}>")
    blocks = _group_top_level(builder.root.children)
    return "\n\n".join(_render_block(block, on_loss) for block in blocks)


def _group_top_level(children: list[_Node | str]) -> list[_Node]:
    """Split top-level children into p/ul/ol/heading blocks, wrapping stray inline
    content in an implicit paragraph and dropping insignificant top-level whitespace."""
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
            child.tag in ("p", "ul", "ol") or child.tag in _HEADING_TAGS
        ):
            flush()
            blocks.append(child)
        else:
            buffer.append(child)
    flush()
    return blocks


def _render_block(node: _Node, on_loss: Callable[[str], None] | None = None) -> str:
    if node.tag == "p":
        return _render_inline(node.children, on_loss)
    if node.tag in _HEADING_TAGS:
        return "#" * int(node.tag[1]) + " " + _render_inline(node.children, on_loss)
    if node.tag in ("ul", "ol"):
        return _render_list_node(node, on_loss)
    raise ConverterError(f"unsupported block-level tag: <{node.tag}>")


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


def _render_list_node(node: _Node, on_loss: Callable[[str], None] | None = None) -> str:
    is_ol = node.tag == "ol"
    n = _ol_start(node, on_loss) if is_ol else 1
    lines = []
    for li in node.children:
        if isinstance(li, str):
            if li.strip() == "":
                continue
            raise ConverterError(f"stray text directly inside <{node.tag}> (expected <li>)")
        if li.tag != "li":
            raise ConverterError(f"unsupported <{node.tag}> child: <{li.tag}>")
        head, nested = _render_li(li, on_loss)
        marker = f"{n}. " if is_ol else "- "
        lines.append(marker + head)
        if is_ol:
            n += 1
        lines.extend("  " + m for m in nested)
    return "\n".join(lines)


_LIST_TAGS = ("ul", "ol")


def _render_li(
    li: _Node, on_loss: Callable[[str], None] | None = None
) -> tuple[str, list[str]]:
    """Render one ``<li>``'s content, unwrapping the ``<p>`` GW wraps item content in.

    Returns ``(head, nested)``: ``head`` is the item's own text; ``nested`` is a flat
    list of single-level, already marker-prefixed (``- `` or ``N. ``, per its own
    tag) sub-item texts for any ``<ul>``/``<ol>`` nested directly inside this
    ``<li>``. A list nested inside one of THOSE (three or more levels deep in the
    source) collapses into that same sub-level rather than a deeper indent, since
    markdown here supports only one nesting convention — each contributing list
    keeps its own marker style, only the indent collapses.

    The corpus impact/mitigation/references fields are ``<ul><li><p>…</p></li></ul>``
    (possibly multiple ``<p>`` siblings, joined with a space); a bare ``<li>`` of
    inline content — including what THIS converter itself emits for a ``<li>`` that
    has a nested list, since it doesn't add a ``<p>`` wrapper — is rendered as one
    contiguous inline run instead, so whitespace round-trips exactly rather than
    being paragraph-joined/stripped.
    """
    has_p = any(isinstance(c, _Node) and c.tag == "p" for c in li.children)
    has_list = any(isinstance(c, _Node) and c.tag in _LIST_TAGS for c in li.children)
    if not has_p and not has_list:
        return _render_inline(li.children, on_loss), []
    nested: list[str] = []
    if not has_p:
        inline_children = [
            c for c in li.children if not (isinstance(c, _Node) and c.tag in _LIST_TAGS)
        ]
        head = _render_inline(inline_children, on_loss)
        for child in li.children:
            if isinstance(child, _Node) and child.tag in _LIST_TAGS:
                nested.extend(_flatten_nested_list(child, on_loss))
        return head, nested
    inline_parts: list[str] = []
    for child in li.children:
        if isinstance(child, _Node) and child.tag in _LIST_TAGS:
            nested.extend(_flatten_nested_list(child, on_loss))
        elif isinstance(child, _Node) and child.tag == "p":
            rendered = _render_inline(child.children, on_loss)
            if rendered:
                inline_parts.append(rendered)
        elif isinstance(child, str):
            if child.strip():
                inline_parts.append(child.strip())
        else:
            rendered = _render_inline([child], on_loss)
            if rendered:
                inline_parts.append(rendered)
    head = " ".join(inline_parts)
    return head, nested


def _flatten_nested_list(lst: _Node, on_loss: Callable[[str], None] | None) -> list[str]:
    """Flatten a ``<ul>``/``<ol>`` nested inside an ``<li>`` — and anything nested
    inside IT — into a flat list of marker-prefixed sub-item texts, all at the one
    supported nesting level (the 2-space indent is applied by the caller)."""
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
        head, deeper = _render_li(li, on_loss)
        if head:
            marker = f"{n}. " if is_ol else "- "
            lines.append(marker + head)
            if is_ol:
                n += 1
        lines.extend(deeper)  # collapse a 3rd+ level into this same sub-level
    return lines


def _render_inline(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None = None
) -> str:
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(n)
        elif n.tag == "br":
            parts.append("\n")
        elif n.tag == "strong":
            parts.append(f"**{_render_inline(n.children, on_loss)}**")
        elif n.tag == "em":
            parts.append(f"*{_render_inline(n.children, on_loss)}*")
        elif n.tag == "code":
            parts.append(f"`{_render_code_text(n.children)}`")
        elif n.tag == "a":
            href = n.attrs.get("href", "")
            rel = n.attrs.get("rel")
            if rel is not None and rel.strip() != "noopener":
                _report_loss(on_loss, f'link rel={rel!r} canonicalized to "noopener" on push')
            target = n.attrs.get("target")
            if target is not None and target.strip() != "_blank":
                _report_loss(
                    on_loss, f'link target={target!r} canonicalized to "_blank" on push'
                )
            title = n.attrs.get("title")
            title_part = f' "{title}"' if title else ""
            parts.append(f"[{_render_inline(n.children, on_loss)}]({href}{title_part})")
        elif n.tag == "span":
            attrs = {k: v for k, v in n.attrs.items() if v}
            if attrs:
                shown = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
                _report_loss(on_loss, f"styling span dropped ({shown})")
            parts.append(_render_inline(n.children, on_loss))
        else:
            raise ConverterError(f"unsupported tag in inline content: <{n.tag}>")
    return "".join(parts)


def _render_code_text(nodes: list[_Node | str]) -> str:
    """<code> content is never inline-parsed, so just flatten its text (unwrapping
    any cosmetic <span>, but rejecting any other nested tag)."""
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(n)
        elif n.tag == "span":
            parts.append(_render_code_text(n.children))
        else:
            raise ConverterError(f"unsupported nested tag inside <code>: <{n.tag}>")
    return "".join(parts)


# --- markdown -> html --------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s")
_ORDERED_ITEM_RE = re.compile(r"^(\d+)\. ")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLOCKQUOTE_RE = re.compile(r"^>\s?")
_TABLE_SEP_RE = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?")
_SETEXT_EQ_RE = re.compile(r"=+")
_RULE_RE = re.compile(r"-{2,}")

_TOKEN_RE = re.compile(
    r"`(?P<code>[^`]*)`"
    # *** before ** and * — ***x*** is strong+em, not strong followed by a stray *.
    r"|\*\*\*(?P<strongem>.+?)\*\*\*"
    r"|\*\*(?P<strong>.+?)\*\*"
    # optional `"title"` — a space then a double-quoted run before the closing `)`;
    # link_url stops at the first space so it can't swallow the title into the href.
    r"|\[(?P<link_text>[^\]]*)\]\((?P<link_url>[^)\s]*)(?:\s+\"(?P<link_title>[^\"]*)\")?\)"
    r"|\*(?P<em1>.+?)\*"
    # __ before _ — same intraword guard as `_em_` below, so `__strong__` isn't read
    # as nested `<em><em>` and snake_case__names/user_id stay untouched.
    r"|(?<!\w)__(?P<strong2>.+?)__(?!\w)"
    # CommonMark's intraword rule for `_em_`: unlike `*em*`, underscore emphasis
    # doesn't fire mid-word — required so snake_case text and underscored URLs
    # (very common as a link's own visible text, see F2) aren't misread as markup.
    r"|(?<!\w)_(?P<em2>.+?)_(?!\w)"
)


def md_to_html(md: str, *, headings: bool = False) -> str:
    """Convert markdown (the tiny closed GW subset) to an HTML fragment. ``headings=True``
    also accepts ATX headings ``# ``–``###### `` (for report-narrative fields)."""
    blocks = re.split(r"\n\s*\n", md)
    return "\n\n".join(_render_md_block(block, headings=headings) for block in blocks)


def _heading_html(line: str) -> str:
    hashes, _, text = line.partition(" ")
    return f"<h{len(hashes)}>{_inline_to_html(text.strip())}</h{len(hashes)}>"


def _render_md_block(block: str, *, headings: bool = False) -> str:
    lines = block.split("\n")
    for line in lines:
        _check_line_whitelist(line, headings=headings)
    if headings and any(_HEADING_RE.match(line) for line in lines):
        # A heading is its own element; consecutive non-heading lines coalesce into a
        # paragraph. GW emits each <hN> standalone, so a clean round-trip stays 1:1.
        out: list[str] = []
        para: list[str] = []
        for line in lines:
            if _HEADING_RE.match(line):
                if para:
                    out.append(f"<p>{'<br>'.join(_inline_to_html(p) for p in para)}</p>")
                    para = []
                out.append(_heading_html(line))
            else:
                para.append(line)
        if para:
            out.append(f"<p>{'<br>'.join(_inline_to_html(p) for p in para)}</p>")
        return "\n\n".join(out)
    if lines and _list_line_kind(lines[0]) == "top" and all(_list_line_kind(x) for x in lines):
        return _render_list_block(lines)
    rendered = [_inline_to_html(line) for line in lines]
    return f"<p>{'<br>'.join(rendered)}</p>"


def _list_line_kind(line: str) -> str | None:
    """``"top"`` for an unindented ``- ``/``* ``/``N. `` item, ``"nested"`` for one
    indented by 2+ spaces (any deeper indent still collapses to the single supported
    sub-level — see module docstring), ``None`` otherwise."""
    if line.startswith("- ") or line.startswith("* ") or _ORDERED_ITEM_RE.match(line):
        return "top"
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) >= 2 and (
        stripped.startswith("- ") or stripped.startswith("* ") or _ORDERED_ITEM_RE.match(stripped)
    ):
        return "nested"
    return None


def _list_marker_kind_and_start(line: str) -> tuple[bool, int]:
    """``(is_ordered, start)`` for a top-level or nested list line — ``start`` is
    the line's own literal number when ordered, else meaningless (1)."""
    stripped = line.lstrip(" ")
    m = _ORDERED_ITEM_RE.match(stripped)
    if m:
        return True, int(m.group(1))
    return False, 1


def _strip_list_marker(line: str) -> str:
    """Strip a line's leading indent and ``- ``/``* ``/``N. `` marker, returning
    just the item's own text content."""
    stripped = line.lstrip(" ")
    if stripped.startswith("- ") or stripped.startswith("* "):
        return stripped[2:]
    m = _ORDERED_ITEM_RE.match(stripped)
    assert m  # callers only pass lines already classified by _list_line_kind
    return stripped[m.end() :]


def _render_list_block(lines: list[str]) -> str:
    """Render a block of ``- ``/``* ``/``N. `` and their 2-space-indented sub-item
    lines into ``<ul>``/``<ol>``, nesting a single list inside an ``<li>`` for that
    item's contiguous run of indented sub-items.

    The outer list's tag is set by the FIRST top-level line's marker; a nested
    group's tag is set by the FIRST line of that contiguous indented run — each
    independently, so ``<ol>`` nested in a ``<ul>`` item (or vice versa) round-trips.
    An ordered list/group's ``start`` is the first item's literal number (an
    explicit ``start="N"`` attribute is only emitted when N != 1); later numbers in
    the same run are accepted but not otherwise significant — canonical sequential
    renumbering happens when the HTML is read back (see ``_render_list_node``)."""
    top_is_ol, top_start = _list_marker_kind_and_start(lines[0])
    items: list[str] = []
    head: str | None = None
    nested_lines: list[str] = []

    def flush() -> None:
        nonlocal head
        if head is None:
            return
        if nested_lines:
            nested_is_ol, nested_start = _list_marker_kind_and_start(nested_lines[0])
            nested_html = "".join(
                f"<li>{_inline_to_html(_strip_list_marker(n))}</li>" for n in nested_lines
            )
            if nested_is_ol:
                attr = f' start="{nested_start}"' if nested_start != 1 else ""
                items.append(f"<li>{head}<ol{attr}>{nested_html}</ol></li>")
            else:
                items.append(f"<li>{head}<ul>{nested_html}</ul></li>")
        else:
            items.append(f"<li>{head}</li>")
        head = None
        nested_lines.clear()

    for line in lines:
        if _list_line_kind(line) == "top":
            flush()
            head = _inline_to_html(_strip_list_marker(line))
        else:  # "nested" — any indent depth collapses to this one sub-level
            nested_lines.append(line)
    flush()
    if top_is_ol:
        attr = f' start="{top_start}"' if top_start != 1 else ""
        return f"<ol{attr}>{''.join(items)}</ol>"
    return f"<ul>{''.join(items)}</ul>"


def _check_line_whitelist(line: str, *, headings: bool = False) -> None:
    stripped = line.strip()
    if _HEADING_RE.match(line) and not headings:
        raise ConverterError(f"unsupported markdown: ATX heading ({line!r})")
    if _IMAGE_RE.search(line):
        raise ConverterError(f"unsupported markdown: image ({line!r})")
    if stripped.startswith("```"):
        raise ConverterError(f"unsupported markdown: fenced code block ({line!r})")
    if _BLOCKQUOTE_RE.match(line):
        raise ConverterError(f"unsupported markdown: blockquote ({line!r})")
    # A markdown table is identified by its separator row (---|---); a bare pipe is
    # not — shell commands inside `code` legitimately contain ` | ` (e.g. `a | nc`).
    if _TABLE_SEP_RE.fullmatch(stripped):
        raise ConverterError(f"unsupported markdown: table ({line!r})")
    if _SETEXT_EQ_RE.fullmatch(stripped):
        raise ConverterError(f"unsupported markdown: setext heading underline ({line!r})")
    if _RULE_RE.fullmatch(stripped):
        raise ConverterError(f"unsupported markdown: setext heading underline or rule ({line!r})")


def _inline_to_html(text: str) -> str:
    """Render inline markdown to HTML. bold/em/link text is recursively re-parsed
    (mirroring ``_render_inline`` on the html->md side) so nested inline tokens —
    a link whose visible text contains ``**bold**``, bold containing `` `code` ``,
    etc. — round-trip instead of the inner markers coming out as literal escaped
    text. ``<code>`` content is never re-parsed (code is verbatim, not markdown)."""
    out = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(_esc(text[pos : m.start()]))
        if m.group("code") is not None:
            out.append(f"<code>{_esc(m.group('code'))}</code>")
        elif m.group("strongem") is not None:
            out.append(f"<strong><em>{_inline_to_html(m.group('strongem'))}</em></strong>")
        elif m.group("strong") is not None:
            out.append(f"<strong>{_inline_to_html(m.group('strong'))}</strong>")
        elif m.group("link_text") is not None:
            href = _esc(m.group("link_url"))
            link_text = _inline_to_html(m.group("link_text"))
            title = m.group("link_title")
            title_attr = f' title="{_esc(title)}"' if title else ""
            out.append(
                f'<a href="{href}"{title_attr} target="_blank" rel="noopener">{link_text}</a>'
            )
        elif m.group("em1") is not None:
            out.append(f"<em>{_inline_to_html(m.group('em1'))}</em>")
        elif m.group("strong2") is not None:
            out.append(f"<strong>{_inline_to_html(m.group('strong2'))}</strong>")
        else:
            out.append(f"<em>{_inline_to_html(m.group('em2'))}</em>")
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)
