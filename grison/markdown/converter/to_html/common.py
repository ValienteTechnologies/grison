"""markdown->html: tiny SyntaxTreeNode helpers shared by ``blocks``/``fence``/
``table`` (kept as their own leaf module so ``blocks.py`` can import
``fence.py``/``table.py`` without a cycle)."""

from __future__ import annotations

from markdown_it.tree import SyntaxTreeNode


def _node_line(node: SyntaxTreeNode) -> int:
    """1-based source line for an error message, from the token's own ``.map``."""
    return (node.map[0] + 1) if node.map else 0


def _inline_children(node: SyntaxTreeNode) -> list[SyntaxTreeNode]:
    return node.children[0].children if node.children else []
