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
  block:  ``<p>`` <-> paragraph, ``<ul><li>`` <-> ``- `` list item
  inline: ``<strong>`` <-> ``**bold**``, ``<code>`` <-> `` `code` ``,
          ``<em>`` <-> ``*em*``/``_em_``, ``<a href>`` <-> ``[text](url)``,
          ``<br>`` <-> a hard line break inside a paragraph
  ``<span>`` is unwrapped (kept, tag dropped) rather than rejected, since
  TinyMCE wraps highlighted text in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


class ConverterError(ValueError):
    """Raised when HTML or markdown outside the tiny closed GW vocabulary is seen."""


_BLOCK_TAGS = {"p", "ul", "li"}
_INLINE_TAGS = {"strong", "code", "em", "a", "br"}
_UNWRAP_TAGS = {"span"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_ALLOWED_TAGS = _BLOCK_TAGS | _INLINE_TAGS | _UNWRAP_TAGS
# Report-narrative fields (report.extraFields) use the same vocabulary as finding
# fields plus headings — the finding converter rejects headings as a corruption
# tripwire, so heading support is opt-in via ``headings=True`` and never loosens the
# strict finding path.


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
                if name == "href":
                    node_attrs["href"] = value or ""
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


def html_to_md(html: str, *, headings: bool = False) -> str:
    """Convert a GW rich-text HTML fragment to markdown. ``headings=True`` also
    accepts ``<h1>``–``<h6>`` (for report-narrative fields, not finding fields)."""
    builder = _TreeBuilder(headings=headings)
    builder.feed(html)
    builder.close()
    if len(builder.stack) != 1:
        raise ConverterError(f"unclosed HTML tag: <{builder.stack[-1].tag}>")
    blocks = _group_top_level(builder.root.children)
    return "\n\n".join(_render_block(block) for block in blocks)


def _group_top_level(children: list[_Node | str]) -> list[_Node]:
    """Split top-level children into p/ul/heading blocks, wrapping stray inline
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
        if isinstance(child, _Node) and (child.tag in ("p", "ul") or child.tag in _HEADING_TAGS):
            flush()
            blocks.append(child)
        else:
            buffer.append(child)
    flush()
    return blocks


def _render_block(node: _Node) -> str:
    if node.tag == "p":
        return _render_inline(node.children)
    if node.tag in _HEADING_TAGS:
        return "#" * int(node.tag[1]) + " " + _render_inline(node.children)
    if node.tag == "ul":
        lines = []
        for li in node.children:
            if isinstance(li, str):
                if li.strip() == "":
                    continue
                raise ConverterError("stray text directly inside <ul> (expected <li>)")
            if li.tag != "li":
                raise ConverterError(f"unsupported <ul> child: <{li.tag}>")
            lines.append("- " + _render_li(li))
        return "\n".join(lines)
    raise ConverterError(f"unsupported block-level tag: <{node.tag}>")


def _render_li(li: _Node) -> str:
    """Render a list item, unwrapping the ``<p>`` GW wraps item content in and
    flattening any nested ``<ul>`` into sibling items.

    The corpus impact/mitigation/references fields are ``<ul><li><p>…</p></li></ul>``;
    a bare ``<li>`` of inline content is rendered directly (preserving inline spacing).
    Nested lists (``<li>…<ul>…</ul></li>``) are flattened — markdown here is
    intentionally single-level, and flattening round-trips stably.
    """
    if not any(isinstance(c, _Node) and c.tag in ("p", "ul") for c in li.children):
        return _render_inline(li.children)
    inline_parts: list[str] = []
    nested: list[str] = []
    for child in li.children:
        if isinstance(child, _Node) and child.tag == "ul":
            nested.extend(_render_block(child).split("\n"))  # already "- …" lines
        elif isinstance(child, _Node) and child.tag == "p":
            rendered = _render_inline(child.children)
            if rendered:
                inline_parts.append(rendered)
        elif isinstance(child, str):
            if child.strip():
                inline_parts.append(child.strip())
        else:
            rendered = _render_inline([child])
            if rendered:
                inline_parts.append(rendered)
    head = " ".join(inline_parts)
    lines = ([head] if head else []) + nested
    return "\n".join(lines)


def _render_inline(nodes: list[_Node | str]) -> str:
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(n)
        elif n.tag == "br":
            parts.append("\n")
        elif n.tag == "strong":
            parts.append(f"**{_render_inline(n.children)}**")
        elif n.tag == "em":
            parts.append(f"*{_render_inline(n.children)}*")
        elif n.tag == "code":
            parts.append(f"`{_render_code_text(n.children)}`")
        elif n.tag == "a":
            href = n.attrs.get("href", "")
            parts.append(f"[{_render_inline(n.children)}]({href})")
        elif n.tag == "span":
            parts.append(_render_inline(n.children))
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
_ORDERED_RE = re.compile(r"^\d+\.\s")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLOCKQUOTE_RE = re.compile(r"^>\s?")
_TABLE_SEP_RE = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?")
_SETEXT_EQ_RE = re.compile(r"=+")
_RULE_RE = re.compile(r"-{2,}")

_TOKEN_RE = re.compile(
    r"`(?P<code>[^`]*)`"
    r"|\*\*(?P<strong>.+?)\*\*"
    r"|\[(?P<link_text>[^\]]*)\]\((?P<link_url>[^)]*)\)"
    r"|\*(?P<em1>.+?)\*"
    r"|_(?P<em2>.+?)_"
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
    if lines and all(_is_list_line(line) for line in lines):
        items = "".join(f"<li>{_inline_to_html(line[2:])}</li>" for line in lines)
        return f"<ul>{items}</ul>"
    rendered = [_inline_to_html(line) for line in lines]
    return f"<p>{'<br>'.join(rendered)}</p>"


def _is_list_line(line: str) -> bool:
    return line.startswith("- ") or line.startswith("* ")


def _check_line_whitelist(line: str, *, headings: bool = False) -> None:
    stripped = line.strip()
    if _HEADING_RE.match(line) and not headings:
        raise ConverterError(f"unsupported markdown: ATX heading ({line!r})")
    if _ORDERED_RE.match(line):
        raise ConverterError(f"unsupported markdown: ordered list ({line!r})")
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
    out = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(_esc(text[pos : m.start()]))
        if m.group("code") is not None:
            out.append(f"<code>{_esc(m.group('code'))}</code>")
        elif m.group("strong") is not None:
            out.append(f"<strong>{_esc(m.group('strong'))}</strong>")
        elif m.group("link_text") is not None:
            href = _esc(m.group("link_url"))
            link_text = _esc(m.group("link_text"))
            out.append(f'<a href="{href}" target="_blank" rel="noopener">{link_text}</a>')
        elif m.group("em1") is not None:
            out.append(f"<em>{_esc(m.group('em1'))}</em>")
        else:
            out.append(f"<em>{_esc(m.group('em2'))}</em>")
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)
