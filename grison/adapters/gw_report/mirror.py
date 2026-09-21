"""``project.md`` rendering — a READ-ONLY mirror of a report's parent GW project.

See :mod:`grison.adapters.gw_report` (this package's ``__init__``) for the
module-level overview of report directories vs. narrative sections.
"""

from __future__ import annotations

from typing import Any

from grison.markdown.converter import ConverterError, html_to_md

_MIRROR_HEADER = "<!-- grison: regenerated every sync — do not edit -->\n\n"


def project_context_to_md(project_rec: dict[str, Any]) -> str:
    """Render ``project.md`` — a READ-ONLY mirror of the report's parent GW project
    (codename, scope, objectives, targets, white cards, collab note), regenerated
    every sync. Sections with no data are omitted entirely. Ported faithfully from
    the pre-engine ``grison.remote.repmap.project_context_to_md`` (golden tests in
    ``tests/test_reports.py`` before this rework; ported onto
    ``tests/test_gw_report.py``) — same Turkish/flag rendering, byte-for-byte."""
    project_rec = project_rec or {}
    client = project_rec.get("client") or {}
    codename = (project_rec.get("codename") or "").strip()
    client_name = (client.get("name") or "").strip()
    start = project_rec.get("startDate") or ""
    end = project_rec.get("endDate") or ""

    lines: list[str] = [_MIRROR_HEADER.strip("\n")]
    lines.append("")
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
            result_txt = (ob.get("result") or "").strip()
            if result_txt:
                lines.append(f"  Result: {result_txt}")

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
            desc_md = _html_to_md_display(wc.get("description") or "").strip()
            if desc_md:
                lines.append("")
                lines.append(desc_md)

    collab_md = _html_to_md_display(project_rec.get("collab_note") or "").strip()
    if collab_md:
        lines.append("")
        lines.append("## Collab note")
        lines.append("")
        lines.append(collab_md)

    return "\n".join(lines).rstrip() + "\n"


def _html_to_md_display(html: str) -> str:
    """``project.md`` is a display-only mirror, never pushed — a construct that
    can't be resolved without a report-scoped evidence resolver (rare for project
    metadata prose) degrades to a lossy-but-visible rendering instead of failing the
    whole mirror; :func:`grison.markdown.converter.html_to_md` already does exactly
    that for an unresolved reference when given no resolver at all only for the
    reference forms — anything else unsupported still raises, so isolate it here."""
    try:
        return html_to_md(html, headings=True)
    except ConverterError:
        return html  # last resort: never crash project.md generation over display prose
