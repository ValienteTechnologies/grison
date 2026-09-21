"""HTML->markdown direction: split into ``blocks`` (top-level/block/list
rendering, including :func:`html_to_md` itself), ``inline`` (inline-content
rendering), and ``evidence`` (embed/cross-reference constructs).
"""

from __future__ import annotations

from grison.markdown.converter.from_html.blocks import html_to_md

__all__ = ["html_to_md"]
