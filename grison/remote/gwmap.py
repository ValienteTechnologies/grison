"""Ghostwriter GraphQL record → house :class:`~grison.model.Finding`, and the
content hash that serves as the 3-way merge base.

GW field names are mostly camelCase but ``replication_steps`` is snake_case (live
schema quirk). GW has no structured CWE field, but CWE ids round-trip through its
tag mechanism using the live convention ``CWE:<n>`` (see :func:`finding_to_gw_tags`
/ :func:`gw_record_to_finding`'s ``tags`` param) — the tag map itself is fetched
separately (``GhostwriterClient.fetch_tag_map``) and passed in by the caller, since
tags join on ``(content_type, object_id)`` rather than living on the finding row.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath

from grison.markdown import html_to_md, md_to_html
from grison.model import Finding, FindingType, Severity, SyncState, is_known_cwe

_HEADING_RE = re.compile(r"<(/?)h[1-6](\s[^>]*)?>", re.IGNORECASE)
# GW's live tag convention for CWE ids: "CWE:<n>" (colon) is canonical on push; pull
# accepts colon or dash plus optional whitespace before the digits, case-insensitive.
_CWE_TAG_RE = re.compile(r"^CWE[-:]\s*(\d+)$", re.IGNORECASE)


def clean_gw_html(html: str) -> str:
    """Normalize GW field-HTML noise: stray headings → paragraphs.

    The corpus has a handful of stray ``<h3>`` (no headings are real field
    structure); everything else is already inside the converter whitelist.
    """
    return _HEADING_RE.sub(lambda m: f"<{m.group(1)}p>", html or "")


def _field_to_md(
    html: str | None, field: str = "", on_loss: Callable[[str], None] | None = None
) -> str:
    if not html or not html.strip():
        return ""
    # Strip to canonical form so Finding -> md -> Finding is identity (the document
    # layer stores prose stripped).
    tagged = (lambda msg: on_loss(f"{field}: {msg}")) if on_loss else None
    return html_to_md(clean_gw_html(html), on_loss=tagged).strip()


def evidence_basename(ev: dict) -> str:
    """The local basename an evidence row wants — shared by the collision counter."""
    return PurePosixPath(ev.get("document") or f"evidence-{ev['id']}").name


def evidence_meta_hash(caption: str, friendly_name: str, description: str) -> str:
    """Hash of an evidence image's caption/friendly_name/description — the per-image
    3-way merge base (Track 1b). These fields sit outside :func:`content_hash` (GW's
    evidence API predates a bulk record-level update), so each image tracks its own
    tiny base in ``EvidenceGwRef.meta`` instead of riding the record's hash."""
    payload = json.dumps(
        {"caption": caption, "friendly_name": friendly_name, "description": description},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _evidence_entries(
    evidence_rows: list[dict], sibling_names: Counter | None = None
) -> list[dict]:
    names = [evidence_basename(ev) for ev in evidence_rows]
    # Disambiguate genuine collisions by GW id; unique names stay plain so a
    # user-added image round-trips to the same path on re-pull. The counter must
    # cover every evidence row that shares the report's evidence/ directory —
    # findings in one report share it, so a per-finding count would let two
    # findings' "screenshot.png" silently overwrite each other on disk.
    dup = sibling_names if sibling_names is not None else Counter(names)
    entries = []
    for ev, name in zip(evidence_rows, names, strict=True):
        local = f"evidence/{ev['id']}-{name}" if dup[name] > 1 else f"evidence/{name}"
        caption = ev.get("caption") or ""
        friendly_name = ev.get("friendlyName") or ""
        description = ev.get("description") or ""
        entries.append(
            {
                "file": local,
                "caption": caption,
                "friendly_name": friendly_name,
                "description": description,
                "gw": {
                    "id": ev["id"],
                    "hash": None,  # image hash stamped after download
                    "meta": evidence_meta_hash(caption, friendly_name, description),
                    "basename": PurePosixPath(local).name,  # rename-guard ground truth
                },
            }
        )
    return entries


def _split_remote_tags(tag_names: list[str]) -> tuple[list[str], list[str]]:
    """Split a record's remote GW tag names into ``(cwe, tags)``. A name shaped like
    ``CWE-79`` / ``CWE:79`` normalizes into ``cwe`` only when it resolves in the
    embedded CWE index; everything else — including CWE-shaped junk that isn't a
    known id — stays verbatim in ``tags`` so nothing GW-authored is invented or
    dropped. Order is preserved (remote order, cwe-matches included in place)."""
    cwe: list[str] = []
    tags: list[str] = []
    for name in tag_names:
        m = _CWE_TAG_RE.match(name.strip())
        norm = f"CWE-{int(m.group(1))}" if m else None
        if norm is not None and is_known_cwe(norm):
            cwe.append(norm)
        else:
            tags.append(name)
    return cwe, tags


def gw_record_to_finding(
    rec: dict,
    *,
    tier: str,
    evidence_rows: list[dict] | None = None,
    evidence_names: Counter | None = None,
    tags: list[str] | None = None,
    on_loss: Callable[[str], None] | None = None,
) -> Finding:
    """Build a (not-yet-synced) house Finding from a GW ``finding``/``reportedFinding`` row.

    ``tags`` is this record's raw GW tag-name list (from
    ``GhostwriterClient.fetch_tag_map()``, keyed by ``(table, id)`` by the caller) —
    split into ``cwe``/``tags`` here (see :func:`_split_remote_tags`).

    ``on_loss``, if given, is called once per dropped/canonicalized construct
    (styling spans, non-canonical link rel/target — see
    :func:`grison.markdown.converter.html_to_md`) across every prose field, each
    message prefixed with the field name it came from (e.g. ``"impact: ..."``)."""
    gw: dict = {
        "table": "finding" if tier == "library" else "reportedFinding",
        "id": rec["id"],
    }
    if tier == "instance":
        gw["report_id"] = rec.get("reportId")

    # score is derived from the vector on read (SSOT) — GW's stored cvssScore is ignored, so
    # a stale/rounded remote score can never ride into the model or the merge comparison.
    vec = (rec.get("cvssVector") or "").strip()
    cvss = {"vector": vec} if vec else None

    affected = (
        _field_to_md(rec.get("affectedEntities"), "affected_entities", on_loss)
        if tier == "instance"
        else None
    )
    ev_entries = (
        _evidence_entries(evidence_rows, evidence_names)
        if (tier == "instance" and evidence_rows)
        else []
    )
    cwe, other_tags = _split_remote_tags(tags or [])

    data = {
        "grison": {"tier": tier, "gw": gw},
        "severity": Severity.from_gw_id(rec["severityId"]),
        "finding_type": FindingType.from_gw_id(rec["findingTypeId"]),
        "cvss": cvss,
        "cwe": cwe,
        "tags": other_tags,
        "affected_entities": affected or None,
        "evidence": ev_entries,
        "title": (rec.get("title") or "").strip() or "Untitled finding",
        "description": _field_to_md(rec.get("description"), "description", on_loss),
        "impact": _field_to_md(rec.get("impact"), "impact", on_loss),
        "mitigation": _field_to_md(rec.get("mitigation"), "mitigation", on_loss),
        "replication_steps": _field_to_md(
            rec.get("replication_steps"), "replication_steps", on_loss
        ),
        "references": _field_to_md(rec.get("references"), "references", on_loss),
    }
    return Finding.model_validate(data)


def _syncable_view(finding: Finding) -> dict:
    """The fields that actually round-trip to Ghostwriter — the merge-base surface.

    Excludes ``evidence`` (reconciled per-image on its own hash) and the ``synced``
    block. Also excludes ``cvss.score``: it's derived from ``cvss_vector`` on every
    read rather than stored, so it can never drift from the vector and including it
    here would be redundant, not protective. Prose is compared as markdown, which is
    stable across md→html→md.
    """
    return {
        "title": finding.title,
        "severity": finding.severity.value,
        "finding_type": finding.finding_type.value,
        "cvss_vector": finding.cvss.vector if finding.cvss else None,
        # sorted: a pure reorder isn't a content change (GW tags are a set, not a list).
        "cwe": sorted(finding.cwe),
        "tags": sorted(finding.tags),
        "affected_entities": finding.affected_entities,
        "description": finding.description,
        "impact": finding.impact,
        "mitigation": finding.mitigation,
        "replication_steps": finding.replication_steps,
        "references": finding.references,
        # only the evidence *file set* — so adding/removing an image triggers a push, but
        # editing a caption/friendly_name/description does NOT falsely mark the finding
        # dirty here. That's not lossy: sync.py reconciles caption/friendly_name/
        # description separately, per image, 3-way against EvidenceGwRef.meta (Track 1b) —
        # same as byte changes under an unchanged filename, reconciled against ``gw.hash``.
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


def finding_to_gw_tags(finding: Finding) -> list[str]:
    """The deterministic tag-string projection pushed via ``setTags``: CWE ids first
    (as ``CWE:<n>``, GW's live convention), then free-form tags verbatim in local
    order — sent as one REPLACE-ALL call, never merged/diffed against remote."""
    return [f"CWE:{c.removeprefix('CWE-')}" for c in finding.cwe] + list(finding.tags)


def stamp_synced(finding: Finding) -> Finding:
    """Stamp ``synced.hash`` (= current content hash) + ``synced.at`` (now)."""
    finding.grison.synced = SyncState(hash=content_hash(finding), at=datetime.now(UTC))
    return finding
