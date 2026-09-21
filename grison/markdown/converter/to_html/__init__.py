"""markdown->html direction: split into ``blocks`` (block-level parsing and
:func:`md_to_html` itself), ``inline`` (inline-content rendering), and
``evidence`` (embed/cross-reference constructs).
"""

from __future__ import annotations

from grison.markdown.converter.to_html.blocks import md_to_html

__all__ = ["md_to_html"]
