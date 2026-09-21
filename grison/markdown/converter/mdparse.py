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

# The "commonmark" preset disables GFM extensions wholesale, including the
# built-in "table" block rule (markdown-it-py ships it, just off by default
# under this preset) — re-enabled explicitly since grison's own grammar now
# supports GFM pipe tables (see the package docstring's whitelist).
_MD = MarkdownIt("commonmark").enable("table")


def _parse_inline_tree(text: str) -> SyntaxTreeNode:
    """Parse a standalone snippet of inline markdown (not a document) — used only
    by ``_md_escape_run``'s round-trip-safety check on the HTML->markdown side."""
    tokens = _MD.parseInline(text, {})
    return SyntaxTreeNode(tokens).children[0]  # unwrap the single "inline" token
