"""markdown->html direction: split into ``blocks`` (block-level parsing and
:func:`md_to_html` itself), ``inline`` (inline-content rendering), ``evidence``
(embed/cross-reference constructs), ``fence`` (fenced code blocks), ``table``
(GFM pipe tables), and ``common`` (tiny SyntaxTreeNode helpers shared by all of
the above, kept as their own leaf so ``blocks`` can import ``fence``/``table``
without a cycle).
"""

from __future__ import annotations

from grison.markdown.converter.to_html.blocks import md_to_html

__all__ = ["md_to_html"]
