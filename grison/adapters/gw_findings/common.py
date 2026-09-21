"""Canonicalization and tag helpers shared, verbatim, by
:class:`~grison.adapters.gw_findings.library.GwLibraryFindingAdapter` and
:class:`~grison.adapters.gw_findings.reported.GwReportedFindingAdapter` — see
:mod:`grison.adapters.gw_findings`'s own docstring for the shape both adapters
share (plain fields, the five prose sections, tags, ``affected_entities``).
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path
from typing import Any

from grison.adapters._gw_common import IndexRefResolver
from grison.engine.filesets import canonical_prose, canonical_remote_prose
from grison.formats import finding as finding_fmt
from grison.index import Index
from grison.markdown.converter import md_to_html
from grison.markdown.refs import RefResolver
from grison.model.cvss import parse_cvss
from grison.model.cwe import is_known_cwe
from grison.model.enums import FindingType, Severity

_SECTIONS = ("description", "impact", "mitigation", "replication_steps", "references")


# --- affected_entities: plain text, always <p>-wrapped on push (see package docstring) --


def _affected_entities_to_html(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "<p></p>"
    return "<p>" + "<br>".join(html_escape(ln) for ln in lines) + "</p>"


def _affected_entities_from_html(html: str) -> str:
    inner = html.strip()
    if inner.startswith("<p>") and inner.endswith("</p>"):
        inner = inner[len("<p>") : -len("</p>")]
    return "\n".join(html_unescape(part) for part in inner.split("<br>") if part.strip())


# --- CWE <-> tag convention (a plain finding/reportedFinding has no structured CWE
# column — the same "CWE:<n>" tag convention the pre-engine v1 gwmap.py used) --------


def _cwe_to_tag(cwe: str) -> str:
    return f"CWE:{cwe.removeprefix('CWE-')}"


def _split_tags(tag_names: Iterable[str]) -> tuple[list[str], list[str]]:
    """``tag_names`` (as fetched from Ghostwriter) -> ``(cwe_ids, plain_tags)``."""
    cwe: list[str] = []
    plain: list[str] = []
    for t in tag_names:
        if t.upper().startswith("CWE:") or t.upper().startswith("CWE-"):
            norm = "CWE-" + t.split(":", 1)[-1].removeprefix("CWE-").removeprefix("cwe-").strip()
            if is_known_cwe(norm):
                cwe.append(norm)
                continue
        plain.append(t)
    return sorted(set(cwe)), plain


def _tags_for(doc: finding_fmt.FindingDoc) -> list[str]:
    return [_cwe_to_tag(c) for c in doc.cwe] + list(doc.tags)


# --- shared plain-field <-> GW mapping ----------------------------------------------

# The RefResolver for a finding's own report — grison.adapters._gw_common.
# IndexRefResolver, the same one narrative sections/notes use (D1). A library
# finding has no report, so it gets this empty stand-in instead of ``None``
# scattered through the canonicalizers below.
_EMPTY_RESOLVER = IndexRefResolver(index=Index(root=Path()), report_dir="")


def _plain_canonical(doc: finding_fmt.FindingDoc) -> dict[str, Any]:
    return {
        "title": doc.title,
        "severity": doc.severity.value,
        "finding_type": doc.finding_type.value,
        "cvss_vector": doc.cvss.vector if doc.cvss is not None else None,
        "cwe": sorted(doc.cwe),
        "tags": sorted(doc.tags),
    }


def _title_of(data: dict[str, Any]) -> str:
    """The title a pulled file gets, and therefore the title the remote canonical
    form must carry: the local parser strips the ``# title`` line and refuses an
    empty one, so a remote title with surrounding whitespace (Ghostwriter never
    trims) or no title at all must normalise the same way on both sides."""
    title = data.get("title")
    return (title.strip() if isinstance(title, str) else "") or "Untitled"


def _remote_plain_canonical(data: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    cwe, plain = _split_tags(tags)
    return {
        "title": _title_of(data),
        "severity": Severity.from_gw_id(data["severityId"]).value,
        "finding_type": FindingType.from_gw_id(data["findingTypeId"]).value,
        "cvss_vector": data.get("cvssVector") or None,
        "cwe": cwe,
        "tags": sorted(plain),
    }


def _sections_canonical(
    doc: finding_fmt.FindingDoc, refs: IndexRefResolver | None
) -> dict[str, Any]:
    resolver = refs if refs is not None else _EMPTY_RESOLVER
    return {f: canonical_prose(getattr(doc, f), resolver) for f in _SECTIONS}


def _remote_sections_canonical(
    data: dict[str, Any], refs: IndexRefResolver | None
) -> dict[str, Any]:
    """D1 ("replacing an image's bytes must re-push every finding referencing
    it, automatically, in the same run"): unlike :func:`_sections_canonical`
    (the LOCAL side, which resolves each embed's id through the live index via
    :func:`~grison.engine.filesets.canonical_prose`), this canonicalizes
    through :func:`~grison.engine.filesets.canonical_remote_prose` — the
    LITERAL id already present in the remote HTML, never re-derived through
    that same live index — so a reupload (which leaves THIS finding's own
    stored HTML untouched) never drifts this payload off ``base`` on its own;
    see that function's docstring for why that's what turns a reupload into a
    same-run PUSH instead of a silent CLEAN or an unresolved-reference PULL
    overwrite."""
    name_to_id = {
        row["friendly_name"]: eid
        for eid, row in (refs.rows().items() if refs is not None else ())
        if row.get("friendly_name")
    }
    # headings=True: a real TipTap editor emits h1-h6 in finding fields too (see
    # grison.markdown.converter's module docstring) — must match render_local's/
    # _gw_fields' own headings=True or this canonical form and the local file's
    # would disagree on any finding whose stored HTML has a heading.
    return {
        f: canonical_remote_prose(data.get(f) or "", headings=True, name_to_id=name_to_id)
        for f in _SECTIONS
    }


def _gw_fields(  # noqa: PLR0913
    doc: finding_fmt.FindingDoc,
    refs: RefResolver | None,
    *,
    instance: bool,
    report_id: int | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "title": doc.title,
        "severityId": doc.severity.gw_id,
        "findingTypeId": doc.finding_type.gw_id,
    }
    for f in _SECTIONS:
        # headings=True: see _remote_sections_canonical's comment above.
        fields[f] = md_to_html(getattr(doc, f), refs=refs, jinja_escape=True, headings=True)
    if doc.cvss is not None:
        fields["cvssVector"] = doc.cvss.vector
        fields["cvssScore"] = parse_cvss(doc.cvss.vector).base_score
    else:
        # cvssVector is a non-nullable column on the real schema (confirmed by the
        # schema-typed fake) — "" is Ghostwriter's own convention for "no vector
        # authored", matching the pre-engine v1 gwmap.py's identical choice.
        fields["cvssVector"] = ""
        fields["cvssScore"] = None
    if instance:
        fields["reportId"] = report_id
        fields["affectedEntities"] = _affected_entities_to_html(doc.affected_entities or "")
    return fields
