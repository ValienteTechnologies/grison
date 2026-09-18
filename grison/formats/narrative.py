"""Format v2 report-narrative section documents: ``findings/reports/<dir>/narrative/<field>.md``.

No frontmatter — the field key is the filename stem, taken from the report's
``extraFieldSpec`` rows (one file per instance-defined rich-text field on the Report
model — see :mod:`grison.adapters.gw_report`). The body is markdown in the
Ghostwriter vocabulary plus headings (``md_to_html(..., headings=True)``); whether it
actually converts is a :mod:`grison.validator` concern (needs the real converter + a
report-scoped ``RefResolver``), not this module's.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class NarrativeDoc(BaseModel):
    """Wraps the section's bare markdown body. A one-field model (rather than a plain
    ``str``) only for interface symmetry with every other format-v2 document type —
    there is no frontmatter here to make ``extra="forbid"`` meaningful."""

    model_config = ConfigDict(extra="forbid")

    body: str = ""


def parse(text: str, *, path: Path) -> NarrativeDoc:
    del path  # no location-derived facts for a narrative section
    return NarrativeDoc(body=text.strip())


def dump(doc: NarrativeDoc) -> str:
    stripped = doc.body.strip()
    return f"{stripped}\n" if stripped else ""
