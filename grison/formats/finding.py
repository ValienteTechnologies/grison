"""Format v2 finding documents: ``findings/library/*.md`` (tier ``library``),
``findings/reports/<dir>/*.md`` (tier ``instance``), and ``findings/inbox/*.md`` (tier
``inbox`` — local-only ``grison parse`` output, triaged by hand into ``library``/
``reports``, but validated with real rules like everywhere else an agent writes).

On-disk shape: YAML frontmatter (the structured fields) + ``# {title}`` + five fixed
``##`` sections, always all five, always in this order:
Description, Impact, Mitigation, Replication Steps, References.

Workspace format v2 carries no machine fields — no ``grison:`` block, no ids, no
hashes, no ``evidence:`` list (D1: the only authored evidence form is an image line in
a section body). Tier is derived from ``path`` alone, never authored. ``affected_entities``
is instance-only (also allowed on ``inbox`` — see below). Library and inbox findings may
not embed images at all (D1: no report exists yet to hold evidence for) — that check
needs the workspace around the document (the evidence folder, other documents sharing a
caption) so it lives in :mod:`grison.validator`, not here.

**No exceptions**: every tier, including ``inbox``, carries zero machine fields.
``grison parse`` used to stamp a narrow ``grison: {gw: {table: reportedFinding}}``
scanner-provenance block on inbox output; that allowance is gone (BRIEF engine step 3
item E) — ``grison parse`` now writes plain v2 documents like everything else, and an
inbox document with an unknown ``grison`` key is rejected the same as any other tier.

Two checks this module deliberately does NOT perform, left to :mod:`grison.validator`:
severity-vs-CVSS-band agreement (needs both fields interpreted together against
:mod:`grison.model.cvss` — and is in fact only enforced on ``library``/``instance``
tiers; a freshly-scanned ``inbox`` finding's severity comes straight from the scanner's
own rating, independent of any CVSS vector attached to it, and routinely disagrees with
the vector's naive base-score band — see the validator's docstring) and whether a
section's markdown actually converts to Ghostwriter HTML (needs the real converter + a
workspace-backed ``RefResolver``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from grison.formats.common import FormatError, translate_validation_error, validate_tags
from grison.markdown import frontmatter as fm
from grison.markdown.frontmatter import DocumentError
from grison.model.cvss import CvssError, parse_cvss
from grison.model.cwe import is_known_cwe, normalize_cwe
from grison.model.enums import FindingType, Severity

Tier = Literal["library", "instance", "inbox"]

# (section header exactly as written in the document, model field name) — fixed order,
# every section required exactly once, no others allowed.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("Description", "description"),
    ("Impact", "impact"),
    ("Mitigation", "mitigation"),
    ("Replication Steps", "replication_steps"),
    ("References", "references"),
)
_HEADER_TO_FIELD = dict(SECTIONS)
_CANONICAL_HEADERS = [h for h, _ in SECTIONS]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindingCvss(_Base):
    """A CVSS 3.0/3.1 base vector. ``score`` is never authored or stored — it is
    always derived from ``vector`` (see :attr:`score`)."""

    vector: str

    @field_validator("vector")
    @classmethod
    def _valid_vector(cls, v: str) -> str:
        try:
            parse_cvss(v)
        except CvssError as e:
            raise ValueError(str(e)) from None
        return v

    @property
    def score(self) -> float:
        return parse_cvss(self.vector).base_score


class FindingDoc(_Base):
    """A format-v2 finding: frontmatter fields + the five prose sections."""

    severity: Severity
    finding_type: FindingType
    cvss: FindingCvss | None = None
    cwe: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    affected_entities: str | None = None  # instance/inbox-only

    title: str = Field(min_length=1)
    description: str = ""
    impact: str = ""
    mitigation: str = ""
    replication_steps: str = ""
    references: str = ""

    @field_validator("cwe")
    @classmethod
    def _known_cwe(cls, raw: list[str]) -> list[str]:
        out: list[str] = []
        for c in raw:
            norm = normalize_cwe(c)
            if norm is None or not is_known_cwe(norm):
                raise ValueError(f"unknown CWE {c!r}: not in the embedded CWE index")
            out.append(norm)
        return out

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, raw: list[str]) -> list[str]:
        return validate_tags(raw)

    @model_validator(mode="after")
    def _tier_constraints(self, info: ValidationInfo) -> FindingDoc:
        tier = (info.context or {}).get("tier")
        if tier == "library" and self.affected_entities:
            raise ValueError("affected_entities is instance-only; not allowed on a library "
                             "finding")
        return self


def tier_of(path: Path) -> Tier:
    """Derive a finding's tier from its location: ``findings/library/...`` ->
    ``library``; ``findings/reports/<dir>/...`` -> ``instance``; ``findings/inbox/...``
    -> ``inbox``. Raises :class:`FormatError` (``kind="bad_location"``) for a path
    under none of those."""
    parts = path.parts
    for i, part in enumerate(parts[:-1]):
        if part == "findings" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt == "library":
                return "library"
            if nxt == "reports":
                return "instance"
            if nxt == "inbox":
                return "inbox"
    raise FormatError("bad_location", str(path))


_H1_RE = re.compile(r"^# (.*)$")
_H2_RE = re.compile(r"^## (.*)$")


def _parse_body(body: str) -> tuple[str, dict[str, str]]:
    """Split the body into ``(title, {field: content})``, enforcing all of FND-009
    through FND-016 structurally (before the pydantic model ever sees the sections)."""
    lines = body.splitlines()
    title: str | None = None
    entries: list[tuple[str, int, list[str]]] = []
    current: list[str] | None = None

    for i, line in enumerate(lines, start=1):
        h2 = _H2_RE.match(line)
        if h2:
            entries.append((h2.group(1).strip(), i, []))
            current = entries[-1][2]
            continue
        if current is not None:
            current.append(line)
            continue
        h1 = _H1_RE.match(line)
        if h1 and title is None:
            title = h1.group(1).strip()
            continue
        if line.strip():
            kind = "missing_title" if title is None else "unexpected_content"
            raise FormatError(kind, line.strip()[:80], line=i)

    if title is None:
        raise FormatError("missing_title", "body must start with '# {title}'")
    if not title:
        raise FormatError("empty_title", "title must not be blank")

    headers_seen: dict[str, int] = {}
    for header, lineno, _content in entries:
        if header not in _HEADER_TO_FIELD:
            raise FormatError("unknown_section", header, line=lineno)
        if header in headers_seen:
            raise FormatError("duplicate_section", header, line=lineno)
        headers_seen[header] = lineno

    missing = [h for h in _CANONICAL_HEADERS if h not in headers_seen]
    if missing:
        raise FormatError("missing_section", missing[0])

    found_order = [header for header, _lineno, _content in entries]
    for idx, (actual, expected) in enumerate(zip(found_order, _CANONICAL_HEADERS, strict=True)):
        if actual != expected:
            raise FormatError(
                "sections_out_of_order",
                f"expected '## {expected}' here, found '## {actual}'",
                line=entries[idx][1],
            )

    sections = {_HEADER_TO_FIELD[h]: "\n".join(c).strip() for h, _lineno, c in entries}
    return title, sections


def parse(text: str, *, path: Path) -> FindingDoc:
    """Parse a finding document. Tier is derived from ``path`` (D3/D4 — location, not
    frontmatter, decides identity-adjacent facts)."""
    tier = tier_of(path)
    try:
        meta, body = fm.split(text)
    except DocumentError as e:
        raise FormatError("bad_frontmatter", str(e)) from e
    title, sections = _parse_body(body)
    data: dict[str, object] = dict(meta)
    data["title"] = title
    data.update(sections)
    try:
        return FindingDoc.model_validate(data, context={"tier": tier})
    except ValidationError as e:
        raise translate_validation_error(
            e,
            text=text,
            root_kind_map={
                "affected_entities": "affected_entities_on_library",
            },
        ) from e


def dump(doc: FindingDoc) -> str:
    """Render a :class:`FindingDoc` back to its document text. ``dump(parse(x)) == x``
    up to the two things ``parse`` doesn't retain byte-for-byte: whitespace inside a
    section body is stripped, and frontmatter key order is canonicalized."""
    meta: dict[str, object] = {}
    meta["severity"] = doc.severity.value
    meta["finding_type"] = doc.finding_type.value
    if doc.cvss is not None:
        meta["cvss"] = {"vector": doc.cvss.vector}
    if doc.cwe:
        meta["cwe"] = doc.cwe
    if doc.tags:
        meta["tags"] = doc.tags
    if doc.affected_entities:
        meta["affected_entities"] = doc.affected_entities

    parts = [f"# {doc.title}", ""]
    for header, field in SECTIONS:
        parts.append(f"## {header}")
        content = getattr(doc, field).strip()
        if content:
            parts.extend(["", content])
        parts.append("")
    return fm.dump(meta, "\n".join(parts))
