"""HTML->markdown: fenced code blocks (module docstring, "Special forms"/
fence). Accepts every real Ghostwriter shape variant: ``<pre>`` with or
without an inner ``<code>``, any ``class``/``spellcheck`` attribute (the
canonical push shape's own ``spellcheck="false"``/``class="language-x"`` are
never reported as dropped — see ``grammar.py``'s ``_KEEP_ATTRS`` — anything
else on either tag still goes through the ordinary generic on_loss path), and
marks (``<strong>``/``<em>``/``<code>``/``<span>``) nested inside — flattened
to plain text (a fenced code block has no inline markup at all), reported via
``on_loss`` since real visual formatting is lost.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.jinja import _RawScanState, _unwrap_jinja_escapes
from grison.markdown.converter.mdtext import _backtick_fence
from grison.markdown.converter.nodes import _Node, _report_dropped_attrs, _report_loss

_LANG_CLASS_RE = re.compile(r"(?:^|\s)language-(\S+)")
_FLATTENED_MARK_TAGS = ("strong", "em", "code", "span")


def _extract_lang(class_attr: str | None) -> str | None:
    if not class_attr:
        return None
    m = _LANG_CLASS_RE.search(class_attr)
    return m.group(1) if m else None


def _fence_marker_for(text: str) -> str:
    """A backtick fence at least 3 long (CommonMark's own minimum for a FENCED
    code block, unlike an inline code span's single-backtick minimum) and one
    longer than the longest run of backticks already in ``text`` — so no line
    inside the block can ever be mistaken for (or accidentally close against)
    the fence itself. Shares its "longest run plus one" scan with
    ``mdtext._fence_code``'s own inline-code-span fence via
    :func:`_backtick_fence`, the two differing only in their minimum length."""
    return _backtick_fence(text, 3)


def _flatten_fence_content(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None, raw_state: _RawScanState
) -> str:
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(_unwrap_jinja_escapes(n, raw_state))
        elif isinstance(n, _Node) and n.tag == "br":
            parts.append("\n")
        elif isinstance(n, _Node) and n.tag in _FLATTENED_MARK_TAGS:
            if n.tag != "span":
                _report_loss(
                    on_loss,
                    f"<{n.tag}> formatting inside a code block flattened to plain text "
                    "(a fenced code block has no inline markup)",
                )
            parts.append(_flatten_fence_content(n.children, on_loss, raw_state))
        else:
            tagname = n.tag if isinstance(n, _Node) else n
            raise ConverterError(f"unsupported tag inside <pre>/<code>: <{tagname}>")
    return "".join(parts)


def _render_pre_node(
    node: _Node, on_loss: Callable[[str], None] | None, raw_state: _RawScanState
) -> str:
    _report_dropped_attrs(node, on_loss)
    code_children = [c for c in node.children if isinstance(c, _Node) and c.tag == "code"]
    if len(code_children) > 1:
        raise ConverterError("unsupported <pre>: more than one <code> child")
    if code_children:
        code_node = code_children[0]
        others = [
            c
            for c in node.children
            if c is not code_node and not (isinstance(c, str) and c.strip() == "")
        ]
        if others:
            raise ConverterError("unsupported <pre>: content outside its <code> child")
        _report_dropped_attrs(code_node, on_loss)
        text = _flatten_fence_content(code_node.children, on_loss, raw_state)
        lang = _extract_lang(code_node.attrs.get("class")) or _extract_lang(node.attrs.get("class"))
    else:
        text = _flatten_fence_content(node.children, on_loss, raw_state)
        lang = _extract_lang(node.attrs.get("class"))
    fence = _fence_marker_for(text)
    opening = f"{fence}{lang}" if lang else fence
    body = text if (text == "" or text.endswith("\n")) else text + "\n"
    return f"{opening}\n{body}{fence}"
