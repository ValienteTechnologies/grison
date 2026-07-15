"""Ghostwriter GraphQL record → house :class:`~grison.model.Finding`, and the
content hash that serves as the 3-way merge base.

GW field names are mostly camelCase but ``replication_steps`` is snake_case (live
schema quirk). GW has **no** structured CWE field (CWE lives in references prose)
and tags live in a separate table, so pulled findings carry ``cwe: []`` / ``tags:
[]`` — a documented mirror gap, not data loss in the round-tripped fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath

from grison.markdown import html_to_md, md_to_html
from grison.model import Finding, FindingType, Severity, SyncState

_HEADING_RE = re.compile(r"<(/?)h[1-6](\s[^>]*)?>", re.IGNORECASE)


def clean_gw_html(html: str) -> str:
    """Normalize GW field-HTML noise: stray headings → paragraphs.

    The corpus has a handful of stray ``<h3>`` (no headings are real field
    structure); everything else is already inside the converter whitelist.
    """
    return _HEADING_RE.sub(lambda m: f"<{m.group(1)}p>", html or "")


def _field_to_md(html: str | None) -> str:
    if not html or not html.strip():
        return ""
    # Strip to canonical form so Finding -> md -> Finding is identity (the document
    # layer stores prose stripped).
    return html_to_md(clean_gw_html(html)).strip()


def _evidence_entries(evidence_rows: list[dict]) -> list[dict]:
    names = [
        PurePosixPath(ev.get("document") or f"evidence-{ev['id']}").name for ev in evidence_rows
    ]
    dup = Counter(names)
    entries = []
    for ev, name in zip(evidence_rows, names, strict=True):
        # Disambiguate only genuine collisions (two rows on this finding sharing a
        # basename, e.g. two "screenshot.png") by GW id; unique names stay plain so
        # a user-added image round-trips to the same path on re-pull.
        local = f"evidence/{ev['id']}-{name}" if dup[name] > 1 else f"evidence/{name}"
        entries.append(
            {
                "file": local,
                "caption": ev.get("caption") or "",
                "friendly_name": ev.get("friendlyName") or "",
                "gw": {"id": ev["id"], "hash": None},  # image hash stamped after download
            }
        )
    return entries


def gw_record_to_finding(
    rec: dict,
    *,
    tier: str,
    evidence_rows: list[dict] | None = None,
) -> Finding:
    """Build a (not-yet-synced) house Finding from a GW ``finding``/``reportedFinding`` row."""
    gw: dict = {
        "table": "finding" if tier == "library" else "reportedFinding",
        "id": rec["id"],
    }
    if tier == "instance":
        gw["report_id"] = rec.get("reportId")

    vec = (rec.get("cvssVector") or "").strip()
    cvss = {"vector": vec, "score": rec.get("cvssScore")} if vec else None

    affected = _field_to_md(rec.get("affectedEntities")) if tier == "instance" else None
    ev_entries = _evidence_entries(evidence_rows) if (tier == "instance" and evidence_rows) else []

    data = {
        "grison": {"tier": tier, "gw": gw},
        "severity": Severity.from_gw_id(rec["severityId"]),
        "finding_type": FindingType.from_gw_id(rec["findingTypeId"]),
        "cvss": cvss,
        "affected_entities": affected or None,
        "evidence": ev_entries,
        "title": (rec.get("title") or "").strip() or "Untitled finding",
        "description": _field_to_md(rec.get("description")),
        "impact": _field_to_md(rec.get("impact")),
        "mitigation": _field_to_md(rec.get("mitigation")),
        "replication_steps": _field_to_md(rec.get("replication_steps")),
        "references": _field_to_md(rec.get("references")),
    }
    return Finding.model_validate(data)


def _syncable_view(finding: Finding) -> dict:
    """The fields that actually round-trip to Ghostwriter — the merge-base surface.

    Excludes ``cwe``/``tags`` (GW has no column for them), ``evidence`` (reconciled
    per-image on its own hash), and the ``synced`` block. Prose is compared as
    markdown, which is stable across md→html→md.
    """
    return {
        "title": finding.title,
        "severity": finding.severity.value,
        "finding_type": finding.finding_type.value,
        "cvss_vector": finding.cvss.vector if finding.cvss else None,
        "affected_entities": finding.affected_entities,
        "description": finding.description,
        "impact": finding.impact,
        "mitigation": finding.mitigation,
        "replication_steps": finding.replication_steps,
        "references": finding.references,
        # only the evidence *file set* — so adding/removing an image triggers a push, but
        # editing a caption/friendly_name (which GW's upload API can't update in place)
        # does NOT falsely mark the finding dirty and then get reverted by the next pull.
        # Byte changes under an unchanged filename are invisible to this hash by design —
        # sync.py reconciles those separately, per-image, against the stamped ``gw.hash``.
        "evidence": sorted(e.file for e in finding.evidence),
    }


def content_hash(finding: Finding) -> str:
    """The 3-way merge base: a stable hash of the finding's Ghostwriter-syncable content."""
    payload = json.dumps(_syncable_view(finding), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


_GW_FIELD_KEYS = (
    "title", "severityId", "findingTypeId", "cvssScore", "cvssVector",
    "description", "impact", "mitigation", "references", "replication_steps",
)
_GW_INSTANCE_KEYS = ("reportId", "affectedEntities")


def gw_pre_image(rec: dict, *, tier: str) -> dict:
    """The settable columns of a remote record, verbatim — the rollback pre-image."""
    keys = _GW_FIELD_KEYS + (_GW_INSTANCE_KEYS if tier == "instance" else ())
    return {k: rec.get(k) for k in keys}


def finding_to_gw_fields(finding: Finding) -> dict:
    """The Ghostwriter column dict for insert/update (prose markdown → field HTML)."""

    def html(md: str) -> str:
        return md_to_html(md) if md.strip() else ""

    fields: dict = {
        "title": finding.title,
        "severityId": finding.severity.gw_id,
        "findingTypeId": finding.finding_type.gw_id,
        "cvssVector": finding.cvss.vector if finding.cvss else "",
        "cvssScore": finding.cvss.score if finding.cvss else None,
        "description": html(finding.description),
        "impact": html(finding.impact),
        "mitigation": html(finding.mitigation),
        "references": html(finding.references),
        "replication_steps": html(finding.replication_steps),
    }
    if finding.grison.tier == "instance":
        fields["reportId"] = finding.grison.gw.report_id
        fields["affectedEntities"] = html(finding.affected_entities or "")
    return fields


def stamp_synced(finding: Finding) -> Finding:
    """Stamp ``synced.hash`` (= current content hash) + ``synced.at`` (now)."""
    finding.grison.synced = SyncState(hash=content_hash(finding), at=datetime.now(UTC))
    return finding
