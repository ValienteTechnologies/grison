"""The tiny HTML tree the html->markdown side builds from a GW rich-text
fragment (:class:`_Node`, built by :class:`_TreeBuilder`), plus the two
``on_loss``-reporting helpers shared across the whole converter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.grammar import _ALLOWED_TAGS, _HEADING_TAGS, _KEEP_ATTRS


def _report_loss(on_loss: Callable[[str], None] | None, msg: str) -> None:
    if on_loss:
        on_loss(msg)


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
        keep = _KEEP_ATTRS.get(tag, ())
        node_attrs: dict[str, str] = {}
        dropped: list[str] = []
        for name, value in attrs:
            if name in keep:
                node_attrs[name] = value or ""
            elif value:
                dropped.append(f"{name}={value!r}")
        if dropped:
            node_attrs["_dropped"] = " ".join(dropped)
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


def _report_dropped_attrs(node: _Node, on_loss: Callable[[str], None] | None) -> None:
    dropped = node.attrs.get("_dropped")
    if dropped:
        _report_loss(on_loss, f"<{node.tag}> attribute(s) dropped: {dropped.strip()}")
