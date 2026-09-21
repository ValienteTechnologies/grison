"""The one shared markdown-it-py instance, plus the standalone-inline-snippet
parse helper both directions need. Kept as its own leaf module (no other
converter-internal imports) specifically to break the import cycle between
the html->markdown side's ``_parses_as_plain_text`` (needs a markdown parse to
verify a text run round-trips as plain text unescaped) and the markdown->html
side's own parser instance.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

_MD = MarkdownIt("commonmark")


def _parse_inline_tree(text: str) -> SyntaxTreeNode:
    """Parse a standalone snippet of inline markdown (not a document) — used only
    by ``_md_escape_run``'s round-trip-safety check on the HTML->markdown side."""
    tokens = _MD.parseInline(text, {})
    return SyntaxTreeNode(tokens).children[0]  # unwrap the single "inline" token
