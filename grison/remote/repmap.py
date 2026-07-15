"""Ghostwriter report ⇄ local report mirror (metadata + narrative sections).

A report's structured lifecycle — project/client, dates, ``complete``/``archived``/
``delivered`` — is Ghostwriter-owned and mirrored **read-only** into ``.report.yml``
(grison never creates or manages reports). Its narrative is the two-way surface:
``report.extraFields`` is an instance-defined jsonb map of rich-text sections
(``executive_summary``, ``methodology``, … — whatever the report's field spec
defines), and each key becomes an editable markdown file under ``narrative/``,
reconciled 3-way per section. extraFields values are HTML in the finding vocabulary
plus headings, so the narrative converter runs in ``headings=True`` mode; the merge
base is the section's markdown, which is a stable fixed point across md→html→md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grison.markdown.converter import html_to_md, md_to_html

REPORT_META = ".report.yml"
NARRATIVE_DIR = "narrative"


@dataclass
class ReportSection:
    key: str
    body: str = ""  # markdown
    synced_hash: str | None = None  # merge base (hash of the last-synced markdown)


@dataclass
class ReportDoc:
    report_id: int
    title: str
    meta: dict = field(default_factory=dict)  # project/client/status/dates — read-only mirror
    sections: dict[str, ReportSection] = field(default_factory=dict)
    raw_extra_fields: dict = field(default_factory=dict)  # verbatim GW jsonb, for lossless push


def section_hash(md: str) -> str:
    return "sha256:" + hashlib.sha256(md.strip().encode()).hexdigest()


def html_section_to_md(html: str) -> str:
    """GW extraField HTML → canonical narrative markdown (the merge-base surface)."""
    return html_to_md(html or "", headings=True).strip()


def md_section_to_html(md: str) -> str:
    """Narrative markdown → GW extraField HTML for push."""
    return md_to_html(md.strip(), headings=True)


def report_from_record(rec: dict) -> ReportDoc:
    """Build a ReportDoc from a GW report row (metadata + extraFields sections)."""
    project = rec.get("project") or {}
    client = project.get("client") or {}
    meta = {
        "project": {
            "id": project.get("id"),
            "client": {
                "id": client.get("id"),
                "name": client.get("name"),
                "short_name": client.get("shortName"),
            },
            "start_date": project.get("startDate"),
            "end_date": project.get("endDate"),
        },
        "status": {
            "complete": rec.get("complete"),
            "archived": rec.get("archived"),
            "delivered": rec.get("delivered"),
        },
        "dates": {"creation": rec.get("creation"), "last_update": rec.get("last_update")},
    }
    raw = dict(rec.get("extraFields") or {})
    sections: dict[str, ReportSection] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            continue  # extraFields is text-valued for narrative; skip any non-string key
        sections[key] = ReportSection(key=key, body=html_section_to_md(value))
    return ReportDoc(
        report_id=rec["id"], title=rec.get("title") or "", meta=meta,
        sections=sections, raw_extra_fields=raw,
    )


def meta_to_yaml(doc: ReportDoc, section_hashes: dict[str, str]) -> str:
    """Serialize ``.report.yml`` — the read-only metadata mirror plus the per-section
    merge base. The narrative bodies live in their own files, not here."""
    fm = {
        "grison": {"kind": "report", "gw": {"report_id": doc.report_id}},
        "title": doc.title,
        **doc.meta,
        "sections": {k: {"hash": h} for k, h in sorted(section_hashes.items())},
    }
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)


def read_local_meta(report_dir: Path) -> tuple[int | None, dict[str, str]]:
    """Return ``(report_id, {section_key: base_hash})`` from a report dir's
    ``.report.yml`` — the merge base for reconcile. Missing file → ``(None, {})``."""
    path = report_dir / REPORT_META
    if not path.exists():
        return None, {}
    fm = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gw = (fm.get("grison") or {}).get("gw") or {}
    bases = {k: (v or {}).get("hash") for k, v in (fm.get("sections") or {}).items()}
    return gw.get("report_id"), bases


def read_local_section(report_dir: Path, key: str) -> str | None:
    """Local markdown body for a section, or ``None`` if the file is absent."""
    path = report_dir / NARRATIVE_DIR / f"{key}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def section_path(report_dir: Path, key: str) -> Path:
    return report_dir / NARRATIVE_DIR / f"{key}.md"
