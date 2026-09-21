"""HTML->markdown direction: split into ``blocks`` (top-level/block/list/
blockquote rendering, including :func:`html_to_md` itself), ``inline``
(inline-content rendering), ``evidence`` (embed/cross-reference constructs),
``fence`` (fenced code blocks), and ``table`` (GFM pipe tables, including the
``collab-table-wrapper`` div).
"""

from __future__ import annotations

from grison.markdown.converter.from_html.blocks import html_to_md

__all__ = ["html_to_md"]
