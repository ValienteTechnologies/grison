"""Offline validity of a finding document — a pure function with two callers.

``status`` reports it; ``sync`` (Phase 8) enforces it before any remote write. A
document is valid when it parses into the schema (enums/CVSS/CWE all check out) and
its prose round-trips the Ghostwriter tag whitelist (no tables/ol/img/headings that
would silently corrupt on push). Listed evidence images must exist on disk.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from grison.markdown import (
    ConverterError,
    DocumentError,
    markdown_to_finding,
    md_to_html,
)
from grison.model import Finding

_PROSE_FIELDS = ("description", "impact", "mitigation", "replication_steps", "references")


def validate_body(finding: Finding) -> list[str]:
    """Whitelist check: every prose section must round-trip to GW HTML."""
    errors: list[str] = []
    for field in _PROSE_FIELDS:
        content: str = getattr(finding, field)
        if content.strip():
            try:
                md_to_html(content)
            except ConverterError as e:
                errors.append(f"{field}: {e}")
    return errors


def validate_text(text: str) -> list[str]:
    """Validate a document's content (schema + whitelist). Empty list == valid."""
    try:
        finding = markdown_to_finding(text)
    except (DocumentError, ValidationError) as e:
        return [_first_line(e)]
    return validate_body(finding)


def validate_file(path: Path) -> list[str]:
    """Validate a document file, plus that its listed evidence images exist on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"cannot read: {e}"]
    errors = validate_text(text)
    if errors:
        return errors
    # Re-parse (cheap) to check evidence files relative to this document's dir.
    finding = markdown_to_finding(text)
    for item in finding.evidence:
        if not (path.parent / item.file).exists():
            errors.append(f"evidence file missing on disk: {item.file}")
    return errors


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0]
