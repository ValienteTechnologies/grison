"""Ghostwriter findings, format v2 (BRIEF task D): library findings (``gw.finding``,
``findings/library/*.md``) and reported findings (``gw.reportedFinding``, ONE FILE
PER FINDING directly inside an indexed ``gw.report`` directory).

Two :class:`~grison.engine.adapter.Adapter` implementations sharing the same
canonicalization helpers below. A reported finding's identity comes from the
index (D3) — never from a directory-name prefix; moving a reported finding's
FILE to another report directory is a real MOVE (:mod:`grison.engine.identity`
pairs it across the whole ``gw.reportedFinding`` scope and
:mod:`grison.engine.apply`'s MOVE handling re-parents it, ``reportId`` included,
in the SAME update call the content push would have made — see
:meth:`GwReportedFindingAdapter.update`). A library <-> report move is NOT a
move at all: a library finding has no ``reportId``/affected_entities/evidence
slot to move INTO, and a reported finding cannot become a template with no
report — BRIEF: "library <-> report moves are CREATE + DELETE" — the validator
already rejects a library finding with `affected_entities` or an evidence
embed (FND-008/REF-005), so grison never needs to guess: a document that moved
from ``findings/library/`` to ``findings/reports/<dir>/`` (or back) is simply
unindexed at its new path (:mod:`grison.engine.identity` only ever pairs within
ONE kind's scope, and library/reportedFinding are different kinds/different
scan roots), so it lands on CREATE, and the old path lands on DELETE_REMOTE —
exactly the "CREATE + DELETE" the brief calls for, with no special-casing here.

Evidence embeds (D1): every prose section goes through
:func:`grison.engine.filesets.canonical_prose`/:func:`grison.markdown.converter.
md_to_html` with a :class:`GwRefResolver` built from the index's ``gw.evidence``
entries for the finding's own report (a library finding gets no resolver at
all — BRIEF: "a library finding cannot carry affected_entities/evidence" — an
embed found in one is a validator failure (REF-005) that the apply loop's
validation gate turns into INVALID before ``create``/``update`` is ever
called; ``refs=None`` is still safe defense in depth: any surviving embed
raises ``ConverterError`` rather than being silently dropped).

``position`` (Int!, scoped per report + severity band): read and carried
through untouched on every update (the ``_set`` payload never includes it, so
Hasura's partial update leaves it exactly where it was — grison never fights a
human's manual drag-and-drop reorder in Ghostwriter's own UI); a freshly
created reported finding gets the next free position in its own report+severity
band (one more than the current max, 0 if the band is empty) — "append at the
end of the band".

``affected_entities`` (instance-only) is always pushed ``<p>``-wrapped, one
``<br>``-separated line per entry (BRIEF/lab: "plain text breaks Ghostwriter's
docx export") — this field is short, unstructured free text (not the tiny
markdown vocabulary the five prose sections use), so it gets its own small
HTML<->text helpers below rather than going through
:mod:`grison.markdown.converter` (a fork, noted in the report: the converter's
grammar has no plain "just wrap it, don't parse markdown" mode, and inventing
one there for a single field seemed like more surface than the one place that
needs it).

``attachFinding`` (evaluated, rejected — BRIEF): Ghostwriter's own
"attach a library finding to a report" action copies an EXISTING library
finding's fields into a new reportedFinding server-side; grison instead always
creates a reportedFinding from whatever LOCAL markdown content is at that path
(the local file, not a library id, is the source of truth for a create) — using
``attachFinding`` would mean grison's own ``create()`` sometimes ignores the
document it was just handed, which is exactly backwards for a tool whose whole
job is "the file is what gets pushed".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWContext
from grison.engine.adapter import AdapterMode
from grison.engine.filesets import canonical_prose
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.formats import finding as finding_fmt
from grison.formats.common import FormatError
from grison.index import Index, IndexKind
from grison.markdown.converter import md_to_html
from grison.markdown.refs import LocalRef, RefResolver, RemoteRef
from grison.model.cvss import parse_cvss
from grison.model.cwe import is_known_cwe
from grison.model.enums import FindingType, Severity
from grison.remote.ghostwriter import GhostwriterClient

_SECTIONS = ("description", "impact", "mitigation", "replication_steps", "references")

# Files that live directly inside a report directory alongside reported findings
# but are NOT one (BRIEF workspace layout: project.md/.report.yml are read-only
# mirrors owned by the report phase, narrative/ and notes/ are subdirectories a
# non-recursive glob("*.md") never reaches anyway) — scan_local must never treat
# one of these as an unindexed reportedFinding candidate (it would otherwise
# misclassify it as a local-only CREATE, which the validator's own IDX-003 on
# that same path then turns into a spurious INVALID for a file this adapter has
# no business touching at all).
_NON_FINDING_REPORT_FILES = frozenset({"project.md"})


# --- affected_entities: plain text, always <p>-wrapped on push (see module docstring) --


def _affected_entities_to_html(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "<p></p>"
    return "<p>" + "<br>".join(html_escape(ln) for ln in lines) + "</p>"


def _affected_entities_from_html(html: str) -> str:
    inner = html.strip()
    if inner.startswith("<p>") and inner.endswith("</p>"):
        inner = inner[len("<p>"): -len("</p>")]
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


# --- the evidence RefResolver (D1) --------------------------------------------------


@dataclass(frozen=True)
class GwRefResolver:
    """Resolves ``evidence/<file>`` against one report's indexed ``gw.evidence``
    entries — built fresh per report by the adapters below from the index (D3:
    identity lives in the index, never in the document) plus that report's
    evidence rows (for the friendly-name a cross-reference embeds — D1: grison
    never authors a friendlyName, but a cross-reference's native span still
    needs SOME stable name to key on, so it reuses whatever Ghostwriter already
    has for that row)."""

    report_dir: PurePosixPath
    index: Index
    evidence_rows: dict[int, dict[str, Any]]  # id -> {filename, caption, description, ...}

    def _id_for_path(self, path: str) -> int | None:
        if not path.startswith("evidence/"):
            return None
        rec = self.index.get(str(self.report_dir / path))
        if rec is None or rec.kind is not IndexKind.GW_EVIDENCE:
            return None
        return rec.id

    def to_remote_id(self, path: str) -> int | None:
        return self._id_for_path(path)

    def to_remote(self, path: str) -> RemoteRef | None:
        eid = self._id_for_path(path)
        if eid is None:
            return None
        row = self.evidence_rows.get(eid, {})
        name = row.get("friendly_name") or PurePosixPath(path).stem
        return RemoteRef("gw-evidence", id=eid, name=name, url=None)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        eid = remote.id
        if eid is None and remote.name is not None:
            eid = next(
                (i for i, r in self.evidence_rows.items() if r.get("friendly_name") == remote.name),
                None,
            )
        if eid is None:
            return None
        path = self.index.path_of(IndexKind.GW_EVIDENCE, eid)
        if path is None:
            return None
        rel = PurePosixPath(path).relative_to(self.report_dir)
        row = self.evidence_rows.get(eid, {})
        return LocalRef(path=str(rel), caption=row.get("caption", ""),
                        description=row.get("description", ""))


_EMPTY_RESOLVER = GwRefResolver(report_dir=PurePosixPath(), index=Index(root=Path()),
                                evidence_rows={})


# --- shared plain-field <-> GW mapping ----------------------------------------------


def _plain_canonical(doc: finding_fmt.FindingDoc) -> dict[str, Any]:
    return {
        "title": doc.title,
        "severity": doc.severity.value,
        "finding_type": doc.finding_type.value,
        "cvss_vector": doc.cvss.vector if doc.cvss is not None else None,
        "cwe": sorted(doc.cwe),
        "tags": sorted(doc.tags),
    }


def _remote_plain_canonical(data: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    cwe, plain = _split_tags(tags)
    return {
        "title": data.get("title") or "",
        "severity": Severity.from_gw_id(data["severityId"]).value,
        "finding_type": FindingType.from_gw_id(data["findingTypeId"]).value,
        "cvss_vector": data.get("cvssVector") or None,
        "cwe": cwe,
        "tags": sorted(plain),
    }


def _sections_canonical(
    doc: finding_fmt.FindingDoc, refs: GwRefResolver | None
) -> dict[str, Any]:
    resolver = refs if refs is not None else _EMPTY_RESOLVER
    return {f: canonical_prose(getattr(doc, f), resolver) for f in _SECTIONS}


def _remote_sections_canonical(
    data: dict[str, Any], refs: GwRefResolver | None
) -> dict[str, Any]:
    from grison.markdown.converter import html_to_md

    out: dict[str, Any] = {}
    for f in _SECTIONS:
        md = html_to_md(data.get(f) or "", refs=refs)
        out[f] = canonical_prose(md, refs if refs is not None else _EMPTY_RESOLVER)
    return out


def _gw_fields(  # noqa: PLR0913
    doc: finding_fmt.FindingDoc, refs: RefResolver | None, *, instance: bool,
    report_id: int | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "title": doc.title,
        "severityId": doc.severity.gw_id,
        "findingTypeId": doc.finding_type.gw_id,
    }
    for f in _SECTIONS:
        fields[f] = md_to_html(getattr(doc, f), refs=refs, jinja_escape=True)
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


def _tags_for(doc: finding_fmt.FindingDoc) -> list[str]:
    return [_cwe_to_tag(c) for c in doc.cwe] + list(doc.tags)


# --- library findings (gw.finding) --------------------------------------------------


@dataclass(frozen=True)
class _LibraryDoc:
    doc: finding_fmt.FindingDoc


class GwLibraryFindingAdapter:
    """``findings/library/*.md`` <-> Ghostwriter ``finding`` rows. No evidence, no
    ``reportId`` — a library finding never has a :class:`GwRefResolver` (see
    module docstring); ``ctx`` is a :class:`~grison.remote.ghostwriter.
    GhostwriterClient` directly (no report scoping needed)."""

    kind = "gw.finding"
    mode: AdapterMode = "read-write"

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "findings" / "library"
        if not base.is_dir():
            return
        for md in sorted(base.glob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            rel = PurePosixPath(md.relative_to(root).as_posix())
            raw = md.read_text(encoding="utf-8")
            try:
                doc = finding_fmt.parse(raw, path=Path(rel))
            except FormatError:
                yield LocalDoc(path=rel, doc=None, raw_text=raw)
                continue
            yield LocalDoc(path=rel, doc=_LibraryDoc(doc), raw_text=raw)

    def fetch_remote(self, ctx: GhostwriterClient) -> dict[int, RemoteRecord]:
        out = {}
        tag_map = ctx.fetch_tag_map()
        for row in ctx.fetch_findings():
            tags = tag_map.get(("finding", row["id"]), [])
            out[row["id"]] = RemoteRecord(id=row["id"], data={**row, "_tags": tags})
        return out

    def refetch(self, ctx: GhostwriterClient, id: int) -> RemoteRecord | None:
        row = ctx.finding_by_pk(id)
        if row is None:
            return None
        tags = ctx.fetch_tags_for("finding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def canonical_local(self, doc: _LibraryDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        return {**_plain_canonical(doc.doc), "sections": _sections_canonical(doc.doc, None)}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        tags = data.get("_tags", [])
        return {**_remote_plain_canonical(data, tags),
                "sections": _remote_sections_canonical(data, None)}

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        from grison.markdown.converter import html_to_md

        cwe, plain = _split_tags(data.get("_tags", []))
        doc = finding_fmt.FindingDoc(
            severity=Severity.from_gw_id(data["severityId"]),
            finding_type=FindingType.from_gw_id(data["findingTypeId"]),
            cvss=finding_fmt.FindingCvss(vector=data["cvssVector"]) if data.get("cvssVector")
            else None,
            cwe=cwe, tags=plain, title=data.get("title") or "Untitled",
            **{f: html_to_md(data.get(f) or "") for f in _SECTIONS},
        )
        return finding_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        from grison.sinks.file_sink import slugify

        return PurePosixPath("findings", "library", f"{slugify(data.get('title') or '')}.md")

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        del data
        return current

    def create(self, ctx: GhostwriterClient, doc: _LibraryDoc) -> RemoteRecord:
        fields = _gw_fields(doc.doc, None, instance=False)
        row = ctx.insert_finding(fields)
        ctx.set_tags(row["id"], "finding", _tags_for(doc.doc))
        tags = ctx.fetch_tags_for("finding", row["id"])
        return RemoteRecord(id=row["id"], data={**row, "_tags": tags})

    def update(self, ctx: GhostwriterClient, id: int, doc: _LibraryDoc) -> RemoteRecord:
        fields = _gw_fields(doc.doc, None, instance=False)
        row = ctx.update_finding(id, fields)
        ctx.set_tags(id, "finding", _tags_for(doc.doc))
        tags = ctx.fetch_tags_for("finding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def delete(self, ctx: GhostwriterClient, id: int) -> None:
        ctx.delete_finding(id)

    def restore(self, ctx: GhostwriterClient, preimage: Any) -> RemoteRecord:
        """Undo's inverse: put the pre-image back onto the SAME id when the row
        still exists (undoing a PUSH — the row is edited, not gone), else recreate
        it (undoing a DELETE_REMOTE — the id is gone for good; Ghostwriter has no
        restore-by-id). Same two-way shape as :meth:`BsPageAdapter.restore`."""
        row = preimage
        fields = {k: v for k, v in row.items() if k not in ("id", "_tags")}
        tags = row.get("_tags", [])
        pid = row.get("id")
        if pid is not None and ctx.finding_by_pk(pid) is not None:
            updated = ctx.update_finding(pid, fields)
            ctx.set_tags(pid, "finding", tags)
            return RemoteRecord(id=pid, data={**updated, "_tags": tags})
        new = ctx.insert_finding(fields)
        if tags:
            ctx.set_tags(new["id"], "finding", tags)
        return RemoteRecord(id=new["id"], data={**new, "_tags": tags})

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        del local, remote
        return None

    def remote_label(self, data: Any) -> str:
        title = data.get("title") if isinstance(data, dict) else None
        rid = data.get("id") if isinstance(data, dict) else None
        return f'"{title or "(untitled)"}" (finding {rid})' if rid is not None else \
            f'"{title or "(untitled)"}"'


# --- reported findings (gw.reportedFinding) -----------------------------------------


@dataclass(frozen=True)
class _ReportedDoc:
    doc: finding_fmt.FindingDoc
    report_dir: PurePosixPath


@dataclass
class GwReportedFindingAdapter:
    """Every indexed report's reported findings, as ONE adapter/kind — so
    :mod:`grison.engine.identity`'s move pairing (which only ever pairs within
    one kind's WHOLE scope) can recognise "moved to another report directory"
    as a MOVE, per BRIEF task D (see module docstring). ``ctx`` is a
    :class:`~grison.adapters._gw_common.GWContext`; ``evidence_by_report`` is
    built by the caller from every report's :class:`~grison.adapters.
    gw_evidence.GwEvidenceAdapter` right before this adapter runs, so evidence
    ids/friendly-names/captions used to build each report's
    :class:`GwRefResolver` are current."""

    index: Index
    evidence_by_report: dict[int, dict[int, dict[str, Any]]]
    kind: str = "gw.reportedFinding"
    mode: AdapterMode = "read-write"

    def _resolver(self, report_id: int | None, report_dir: PurePosixPath) -> GwRefResolver:
        rows = self.evidence_by_report.get(report_id, {}) if report_id is not None else {}
        return GwRefResolver(report_dir=report_dir, index=self.index, evidence_rows=rows)

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "findings" / "reports"
        if not base.is_dir():
            return
        for report_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            rel_dir = PurePosixPath(report_dir.relative_to(root).as_posix())
            for md in sorted(report_dir.glob("*.md")):
                if md.name.endswith(".remote.md") or md.name in _NON_FINDING_REPORT_FILES:
                    continue
                rel = PurePosixPath(md.relative_to(root).as_posix())
                raw = md.read_text(encoding="utf-8")
                try:
                    parsed = finding_fmt.parse(raw, path=Path(rel))
                except FormatError:
                    yield LocalDoc(path=rel, doc=None, raw_text=raw)
                    continue
                yield LocalDoc(path=rel, doc=_ReportedDoc(parsed, rel_dir), raw_text=raw)

    def fetch_remote(self, ctx: GWContext) -> dict[int, RemoteRecord]:
        out = {}
        tag_map = ctx.client.fetch_tag_map()
        for row in ctx.client.fetch_reported_findings():
            tags = tag_map.get(("reportedFinding", row["id"]), [])
            out[row["id"]] = RemoteRecord(id=row["id"], data={**row, "_tags": tags})
        return out

    def refetch(self, ctx: GWContext, id: int) -> RemoteRecord | None:
        row = ctx.client.reported_finding_by_pk(id)
        if row is None:
            return None
        tags = ctx.client.fetch_tags_for("reportedFinding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def canonical_local(self, doc: _ReportedDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        resolver = self._resolver(None, doc.report_dir)
        return {**_plain_canonical(doc.doc), "affected_entities": doc.doc.affected_entities or "",
                "sections": _sections_canonical(doc.doc, resolver),
                "report": str(doc.report_dir)}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        resolver = self._resolver(data["reportId"], PurePosixPath(report_dir) if report_dir
                                  else PurePosixPath())
        tags = data.get("_tags", [])
        return {**_remote_plain_canonical(data, tags),
                "affected_entities": _affected_entities_from_html(data.get("affectedEntities")
                                                                   or ""),
                "sections": _remote_sections_canonical(data, resolver),
                # Report membership must be part of the hash, exactly like
                # BsPageAdapter includes book/chapter (grison/adapters/bs_pages.py):
                # without it, a cross-report MOVE with byte-identical content makes
                # `_apply_move`'s `needs_write` compare equal and the finding keeps
                # its OLD reportId on Ghostwriter forever, silently — the bug this
                # field closes. `report_dir` is None only for a reportId the index
                # no longer knows (orphaned data); falling back to "" still makes a
                # real reparent (known dir -> unknown) compare unequal.
                "report": report_dir or ""}

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        from grison.markdown.converter import html_to_md

        report_dir = path.parent
        resolver = self._resolver(data["reportId"], report_dir)
        cwe, plain = _split_tags(data.get("_tags", []))
        doc = finding_fmt.FindingDoc(
            severity=Severity.from_gw_id(data["severityId"]),
            finding_type=FindingType.from_gw_id(data["findingTypeId"]),
            cvss=finding_fmt.FindingCvss(vector=data["cvssVector"]) if data.get("cvssVector")
            else None,
            cwe=cwe, tags=plain, title=data.get("title") or "Untitled",
            affected_entities=_affected_entities_from_html(data.get("affectedEntities") or "")
            or None,
            **{f: html_to_md(data.get(f) or "", refs=resolver) for f in _SECTIONS},
        )
        return finding_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        from grison.sinks.file_sink import slugify

        base = PurePosixPath(report_dir) if report_dir else PurePosixPath("findings", "reports")
        return base / f"{slugify(data.get('title') or '')}.md"

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        if report_dir is None:
            return current
        return PurePosixPath(report_dir) / current.name

    def _end_of_band_position(self, ctx: GWContext, report_id: int, severity_id: int) -> int:
        siblings = [
            r for r in ctx.client.fetch_reported_findings()
            if r.get("reportId") == report_id and r.get("severityId") == severity_id
        ]
        if not siblings:
            return 0
        return int(max(r.get("position", 0) for r in siblings)) + 1

    def create(self, ctx: GWContext, doc: _ReportedDoc) -> RemoteRecord:
        report_id = ctx.report_id_for(doc.report_dir)
        if report_id is None:
            raise LookupError(f"{doc.report_dir} is not an indexed report directory")
        resolver = self._resolver(report_id, doc.report_dir)
        fields = _gw_fields(doc.doc, resolver, instance=True, report_id=report_id)
        fields["position"] = self._end_of_band_position(ctx, report_id, doc.doc.severity.gw_id)
        row = ctx.client.insert_reported_finding(fields)
        ctx.client.set_tags(row["id"], "reportedFinding", _tags_for(doc.doc))
        tags = ctx.client.fetch_tags_for("reportedFinding", row["id"])
        return RemoteRecord(id=row["id"], data={**row, "_tags": tags})

    def update(self, ctx: GWContext, id: int, doc: _ReportedDoc) -> RemoteRecord:
        report_id = ctx.report_id_for(doc.report_dir)
        if report_id is None:
            raise LookupError(f"{doc.report_dir} is not an indexed report directory")
        resolver = self._resolver(report_id, doc.report_dir)
        fields = _gw_fields(doc.doc, resolver, instance=True, report_id=report_id)
        # position deliberately NOT in fields — Hasura's partial `_set` leaves an
        # omitted column untouched, which is exactly "preserved on update" (module
        # docstring); a cross-report MOVE still reparents via `reportId` above.
        row = ctx.client.update_reported_finding(id, fields)
        ctx.client.set_tags(id, "reportedFinding", _tags_for(doc.doc))
        tags = ctx.client.fetch_tags_for("reportedFinding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def delete(self, ctx: GWContext, id: int) -> None:
        ctx.client.delete_reported_finding(id)

    def restore(self, ctx: GWContext, preimage: Any) -> RemoteRecord:
        """Same two-way shape as :meth:`GwLibraryFindingAdapter.restore`: in-place
        update when the id still exists (undoing a PUSH/MOVE_EDIT — including a
        cross-report move, since ``reportId`` is part of the pre-image), recreate
        when it is gone (undoing a DELETE_REMOTE)."""
        row = preimage
        fields = {k: v for k, v in row.items() if k not in ("id", "_tags")}
        tags = row.get("_tags", [])
        pid = row.get("id")
        if pid is not None and ctx.client.reported_finding_by_pk(pid) is not None:
            updated = ctx.client.update_reported_finding(pid, fields)
            ctx.client.set_tags(pid, "reportedFinding", tags)
            return RemoteRecord(id=pid, data={**updated, "_tags": tags})
        new = ctx.client.insert_reported_finding(fields)
        if tags:
            ctx.client.set_tags(new["id"], "reportedFinding", tags)
        return RemoteRecord(id=new["id"], data={**new, "_tags": tags})

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        del local, remote
        return None

    def remote_label(self, data: Any) -> str:
        title = data.get("title") if isinstance(data, dict) else None
        rid = data.get("id") if isinstance(data, dict) else None
        return f'"{title or "(untitled)"}" (finding {rid})' if rid is not None else \
            f'"{title or "(untitled)"}"'
