"""Markdown layer: the HTML⇄markdown converter, and scanner-IR → format-v2 inbox
finding mapping.

The GW field vocabulary is tiny and closed; the converter fails loudly on anything
outside it. A finding's prose fields are markdown; ``##`` section headers are grison
structure that map to Ghostwriter's separate fields (:mod:`grison.formats.finding`).
"""

from __future__ import annotations

from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.markdown.mapping import (
    MappingResult,
    default_finding_type,
    ir_to_finding,
)
from grison.markdown.refs import LocalRef, RefResolver, RemoteRef

__all__ = [
    "ConverterError",
    "LocalRef",
    "MappingResult",
    "RefResolver",
    "RemoteRef",
    "default_finding_type",
    "html_to_md",
    "ir_to_finding",
    "md_to_html",
]
