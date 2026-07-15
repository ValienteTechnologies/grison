"""Markdown layer: the HTML⇄markdown converter, Finding⇄document serialization,
and scanner-IR → house-schema mapping.

The GW field vocabulary is tiny and closed; the converter fails loudly on anything
outside it. A Finding's prose fields are markdown; ``##`` section headers are grison
structure that map to Ghostwriter's separate fields.
"""

from __future__ import annotations

from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.markdown.document import (
    DocumentError,
    finding_to_markdown,
    markdown_to_finding,
)
from grison.markdown.mapping import (
    MappingResult,
    default_finding_type,
    ir_to_finding,
)

__all__ = [
    "ConverterError",
    "DocumentError",
    "MappingResult",
    "default_finding_type",
    "finding_to_markdown",
    "html_to_md",
    "ir_to_finding",
    "markdown_to_finding",
    "md_to_html",
]
