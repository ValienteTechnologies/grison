"""The tier-agnostic ``Finding`` schema (pydantic v2).

One schema whether the finding is a Ghostwriter library template or a report
instance — ``tier`` is metadata. Structured facts live in the model fields (they
serialize to YAML frontmatter in Phase 4); the prose sections (description /
impact / mitigation / replication_steps / references) are markdown strings that
serialize to fixed ``##`` sections. Validation errors are written to read as
guardrail messages ("say exactly what to fix").

Dead GW fields are absent by design: no ``host/network_detection_techniques``,
``finding_guidance``, or ``extra_fields`` (all ~0% populated in the live corpus).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from grison.model.cvss import CvssError, parse_cvss
from grison.model.cwe import cwe_name, is_known_cwe, normalize_cwe
from grison.model.enums import FindingType, Severity


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GwRef(_Base):
    """Pointer back to the Ghostwriter record. ``id`` is null until first synced."""

    table: Literal["finding", "reportedFinding"] | None = None
    id: int | None = None
    report_id: int | None = None  # instances only


class SyncState(_Base):
    """The 3-way merge base — content hash + time of the last sync."""

    hash: str | None = None
    at: datetime | None = None


class GrisonMeta(_Base):
    """The ``grison:`` frontmatter block — kind, tier, GW pointer, sync base."""

    kind: Literal["finding"] = "finding"
    tier: Literal["library", "instance"]
    gw: GwRef = Field(default_factory=GwRef)
    synced: SyncState | None = None

    @model_validator(mode="after")
    def _tier_table_consistent(self) -> GrisonMeta:
        expected = "finding" if self.tier == "library" else "reportedFinding"
        if self.gw.table is None:
            self.gw.table = expected
        elif self.gw.table != expected:
            raise ValueError(
                f"tier '{self.tier}' requires gw.table '{expected}', got '{self.gw.table}'"
            )
        if self.tier == "library" and self.gw.report_id is not None:
            raise ValueError("gw.report_id is instance-only; not allowed on library tier")
        return self


class Cvss(_Base):
    """A CVSS vector (3.0 or 3.1, as-authored) with its base score.

    The vector is validated for grammar/metrics but its version is **not**
    forced to 3.1 — scanners emit 3.0 and grison records them faithfully. The
    score is computed from the vector when not supplied.
    """

    vector: str
    score: float | None = None

    @field_validator("vector")
    @classmethod
    def _valid_vector(cls, v: str) -> str:
        try:
            parse_cvss(v)
        except CvssError as e:
            raise ValueError(str(e)) from None
        return v

    @model_validator(mode="after")
    def _default_score(self) -> Cvss:
        if self.score is None:
            self.score = parse_cvss(self.vector).base_score
        return self


class EvidenceGwRef(_Base):
    """The Ghostwriter ``evidence`` record id + content hash at last sync.

    ``meta`` is the per-image merge base for caption/friendly_name/description (Track
    1b) — these fields sit outside :func:`~grison.remote.gwmap.content_hash` (GW's
    evidence API predates a bulk record-level update), so each image tracks its own
    tiny 3-way base instead. ``basename`` is the filename grison stamped at
    upload/pull time — used to detect a local rename (evidence filenames mirror GW's
    server-managed storage path, so a rename can't itself be pushed; see the rename
    guard in :mod:`grison.remote.sync`).
    """

    id: int
    hash: str | None = None
    meta: str | None = None
    basename: str | None = None


class EvidenceItem(_Base):
    """One image attached to a finding (instances only)."""

    file: str
    caption: str = ""
    friendly_name: str = ""
    description: str = ""
    gw: EvidenceGwRef | None = None


class Finding(_Base):
    """A security finding — library template or report instance, one schema."""

    grison: GrisonMeta
    severity: Severity
    finding_type: FindingType
    cvss: Cvss | None = None
    cwe: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    affected_entities: str | None = None  # instances only
    evidence: list[EvidenceItem] = Field(default_factory=list)  # instances only

    # Body: title + fixed prose sections (markdown strings).
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

    @model_validator(mode="after")
    def _tier_constraints(self) -> Finding:
        if self.grison.tier == "library":
            if self.affected_entities:
                raise ValueError("affected_entities is instance-only; not allowed on library tier")
            if self.evidence:
                raise ValueError("evidence is instance-only; not allowed on library tier")
        return self

    @property
    def cwe_names(self) -> dict[str, str | None]:
        """``{"CWE-16": "Configuration", …}`` — for rendering the References section."""
        return {c: cwe_name(c) for c in self.cwe}
