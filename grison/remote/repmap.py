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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grison.markdown.converter import html_to_md, md_to_html
from grison.sinks.file_sink import slugify

REPORT_META = ".report.yml"
NARRATIVE_DIR = "narrative"
PROJECT_CONTEXT_FILE = "project.md"
NOTES_DIR = "notes"


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
    raw_project: dict = field(default_factory=dict)  # verbatim GW project row, for project.md/notes


def section_hash(md: str) -> str:
    return "sha256:" + hashlib.sha256(md.strip().encode()).hexdigest()


def html_section_to_md(html: str, *, on_loss: Callable[[str], None] | None = None) -> str:
    """GW extraField HTML → canonical narrative markdown (the merge-base surface).

    ``on_loss``, if given, is called once per dropped/canonicalized construct (styling
    spans, non-canonical link rel/target) — see :func:`grison.markdown.html_to_md`."""
    return html_to_md(html or "", headings=True, on_loss=on_loss).strip()


def md_section_to_html(md: str) -> str:
    """Narrative markdown → GW extraField HTML for push."""
    return md_to_html(md.strip(), headings=True)


def report_from_record(
    rec: dict, *, on_loss: Callable[[str, str], None] | None = None
) -> ReportDoc:
    """Build a ReportDoc from a GW report row (metadata + extraFields sections).

    ``on_loss``, if given, is called ``on_loss(section_key, message)`` once per
    dropped/canonicalized construct in that section's HTML (styling spans,
    non-canonical link rel/target)."""
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
        section_on_loss = (lambda msg, key=key: on_loss(key, msg)) if on_loss else None
        body = html_section_to_md(value, on_loss=section_on_loss)
        sections[key] = ReportSection(key=key, body=body)
    return ReportDoc(
        report_id=rec["id"], title=rec.get("title") or "", meta=meta,
        sections=sections, raw_extra_fields=raw, raw_project=project,
    )


def meta_to_yaml(doc: ReportDoc) -> str:
    """Serialize ``.report.yml`` — the read-only metadata mirror (title/project/status/
    dates), regenerated every sync. Holds no merge state: narrative bodies live under
    ``narrative/``, and section merge bases live in the private state store
    (``.grison/state/report/<id>.json``), keyed by report_id — a git checkout of this
    file can never resurrect a stale base."""
    fm = {
        "grison": {"kind": "report", "gw": {"report_id": doc.report_id}},
        "title": doc.title,
        **doc.meta,
    }
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)


def read_local_meta(report_dir: Path) -> int | None:
    """Return the ``report_id`` recorded in a report dir's ``.report.yml`` mirror, or
    ``None`` if the file is absent. Cosmetic only — merge bases live in the state
    store, keyed by the report_id callers already have from the GW record."""
    path = report_dir / REPORT_META
    if not path.exists():
        return None
    fm = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gw = (fm.get("grison") or {}).get("gw") or {}
    return gw.get("report_id")


def read_local_section(report_dir: Path, key: str) -> str | None:
    """Local markdown body for a section, or ``None`` if the file is absent."""
    path = report_dir / NARRATIVE_DIR / f"{key}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def section_path(report_dir: Path, key: str) -> Path:
    return report_dir / NARRATIVE_DIR / f"{key}.md"


# --- project context (read-only mirror, regenerated every sync) ------------


def project_context_to_md(project_rec: dict) -> str:
    """Render ``project.md`` — a READ-ONLY mirror of the report's parent GW project
    (codename, scope, objectives, targets, white cards, collab note), regenerated every
    sync like ``.report.yml``. Sections with no data are omitted entirely. HTML fields
    (white card descriptions, the collab note) go through the same narrative HTML->md
    converter as report sections; a "<p></p>"-ish empty collab note is dropped."""
    project_rec = project_rec or {}
    client = project_rec.get("client") or {}
    codename = (project_rec.get("codename") or "").strip()
    client_name = (client.get("name") or "").strip()
    start = project_rec.get("startDate") or ""
    end = project_rec.get("endDate") or ""

    lines: list[str] = ["<!-- grison: regenerated every sync — do not edit -->", ""]
    lines.append(f"# {codename or client_name or 'Project'}")
    meta_bits = []
    if client_name:
        meta_bits.append(f"**Client:** {client_name}")
    if start or end:
        meta_bits.append(f"**Dates:** {start} – {end}")
    if meta_bits:
        lines.append("")
        lines.extend(meta_bits)

    scopes = project_rec.get("scopes") or []
    if scopes:
        lines.append("")
        lines.append("## Scope")
        for sc in scopes:
            name = sc.get("name") or "Scope"
            flags = []
            if sc.get("disallowed"):
                flags.append("EXCLUDED")
            if sc.get("requiresCaution"):
                flags.append("CAUTION")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append("")
            lines.append(f"### {name}{flag_str}")
            desc = (sc.get("description") or "").strip()
            if desc:
                lines.append("")
                lines.append(desc)
            entries = [
                e.strip()
                for e in (sc.get("scope") or "").replace("\r\n", "\n").split("\n")
                if e.strip()
            ]
            if entries:
                lines.append("")
                lines.extend(f"- {e}" for e in entries)

    objectives = project_rec.get("objectives") or []
    if objectives:
        lines.append("")
        lines.append("## Objectives")
        lines.append("")
        for ob in objectives:
            text = ob.get("objective") or ""
            status = (ob.get("objectiveStatus") or {}).get("objectiveStatus")
            priority = (ob.get("objectivePriority") or {}).get("priority")
            bits = [b for b in (status, priority) if b]
            head = f"- **{text}**"
            if bits:
                head += f" — {' / '.join(bits)}"
            deadline = ob.get("deadline")
            if deadline:
                head += f" (deadline: {deadline})"
            if ob.get("complete") or ob.get("markedComplete"):
                head += " [COMPLETE]"
            lines.append(head)
            desc = (ob.get("description") or "").strip()
            if desc:
                lines.append(f"  {desc}")
            result = (ob.get("result") or "").strip()
            if result:
                lines.append(f"  Result: {result}")

    targets = project_rec.get("targets") or []
    if targets:
        lines.append("")
        lines.append("## Targets")
        lines.append("")
        for t in targets:
            host_ip = " / ".join(x for x in (t.get("hostname"), t.get("ipAddress")) if x)
            marker = " (COMPROMISED)" if t.get("compromised") else ""
            lines.append(f"- {host_ip or 'unknown target'}{marker}")
            desc = (t.get("description") or "").strip()
            if desc:
                lines.append(f"  {desc}")

    whitecards = project_rec.get("whitecards") or []
    if whitecards:
        lines.append("")
        lines.append("## White cards")
        for wc in whitecards:
            title = wc.get("title") or ""
            issued = wc.get("issued") or ""
            lines.append("")
            lines.append(f"### {title}" + (f" — {issued}" if issued else ""))
            desc_md = html_section_to_md(wc.get("description") or "").strip()
            if desc_md:
                lines.append("")
                lines.append(desc_md)

    collab_md = html_section_to_md(project_rec.get("collab_note") or "").strip()
    if collab_md:
        lines.append("")
        lines.append("## Collab note")
        lines.append("")
        lines.append(collab_md)

    return "\n".join(lines).rstrip() + "\n"


# --- project notes (read-only pull mirror; new local files are push candidates) --


def note_to_md(note_rec: dict) -> tuple[str, str]:
    """Render one GW ``projectNote`` as its local mirror: ``<id>-<slug>.md`` with
    identity frontmatter (``grison.gw.note_id``/``project_id``) plus author/timestamp,
    body = note HTML->md. Read-only, regenerated every sync — never hand-edit an
    already-id-stamped note file. ``note_rec`` is the raw GW comment row with a
    ``projectId`` key merged in by the caller (the ``comments`` relation itself carries
    no project id)."""
    note_id = note_rec.get("id")
    body = html_section_to_md(note_rec.get("note") or "").strip()
    user = note_rec.get("user") or {}
    author = (user.get("name") or user.get("username") or "").strip()
    words = body.split()
    slug = slugify(" ".join(words[:6])) if words else "note"
    filename = f"{note_id}-{slug}.md"

    fm: dict = {
        "grison": {
            "kind": "note",
            "gw": {"note_id": note_id, "project_id": note_rec.get("projectId")},
        },
    }
    if author:
        fm["author"] = author
    if note_rec.get("timestamp"):
        fm["timestamp"] = note_rec["timestamp"]
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    content = f"---\n{fm_yaml}\n---\n\n{body}\n" if body else f"---\n{fm_yaml}\n---\n"
    return filename, content


def read_local_note(path: Path) -> tuple[int | None, str]:
    """Parse a ``notes/`` file: returns ``(note_id, body_markdown)``. ``note_id`` is
    ``None`` when the file has no frontmatter, unparseable frontmatter, or no
    ``grison.gw.note_id`` — i.e. a fresh, locally-authored note not yet pushed to
    Ghostwriter (the note-push scan's candidate set)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None, text.strip()
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :]).strip()
            try:
                fm = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                return None, text.strip()
            if not isinstance(fm, dict):
                return None, body
            note_id = ((fm.get("grison") or {}).get("gw") or {}).get("note_id")
            return note_id, body
    return None, text.strip()  # unterminated fence — treat the whole file as body
