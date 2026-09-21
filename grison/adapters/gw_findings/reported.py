"""Every indexed report's reported findings (``gw.reportedFinding``) — see
:mod:`grison.adapters.gw_findings`'s own docstring for the shared shape and the
library/reported CREATE+DELETE move semantics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWContext, IndexRefResolver
from grison.engine.adapter import AdapterMode
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.formats import finding as finding_fmt
from grison.formats.common import FormatError
from grison.index import Index, IndexKind
from grison.model.enums import FindingType, Severity

from .common import (
    _SECTIONS,
    _affected_entities_from_html,
    _gw_fields,
    _plain_canonical,
    _remote_plain_canonical,
    _remote_sections_canonical,
    _sections_canonical,
    _split_tags,
    _tags_for,
    _title_of,
)

# Files that live directly inside a report directory alongside reported findings
# but are NOT one (BRIEF workspace layout: project.md/.report.yml are read-only
# mirrors owned by the report phase, narrative/ and notes/ are subdirectories a
# non-recursive glob("*.md") never reaches anyway) — scan_local must never treat
# one of these as an unindexed reportedFinding candidate (it would otherwise
# misclassify it as a local-only CREATE, which the validator's own IDX-003 on
# that same path then turns into a spurious INVALID for a file this adapter has
# no business touching at all).
_NON_FINDING_REPORT_FILES = frozenset({"project.md"})


@dataclass(frozen=True)
class _ReportedDoc:
    doc: finding_fmt.FindingDoc
    report_dir: PurePosixPath


@dataclass
class GwReportedFindingAdapter:
    """Every indexed report's reported findings, as ONE adapter/kind — so
    :mod:`grison.engine.identity`'s move pairing (which only ever pairs within
    one kind's WHOLE scope) can recognise "moved to another report directory"
    as a MOVE, per BRIEF task D (see package docstring). ``ctx`` is a
    :class:`~grison.adapters._gw_common.GWContext`; ``evidence_by_report`` is
    built by the caller from every report's :class:`~grison.adapters.
    gw_evidence.GwEvidenceAdapter` right before this adapter runs, so evidence
    ids/friendly-names/captions used to build each report's
    :class:`~grison.adapters._gw_common.IndexRefResolver` are current."""

    index: Index
    evidence_by_report: dict[int, dict[int, dict[str, Any]]]
    kind: str = "gw.reportedFinding"
    mode: AdapterMode = "read-write"

    def _resolver(self, report_id: int | None, report_dir: PurePosixPath) -> IndexRefResolver:
        rows = self.evidence_by_report.get(report_id, {}) if report_id is not None else {}
        # IndexRefResolver.report_dir is the bare directory name (workspace-relative
        # under findings/reports/ — see its own field docstring); `report_dir` here
        # is the FULL path (e.g. "findings/reports/<dir>"), so only its final
        # component is what the resolver wants.
        return IndexRefResolver(report_dir=report_dir.name, index=self.index, evidence_rows=rows)

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
        return {
            **_plain_canonical(doc.doc),
            "affected_entities": doc.doc.affected_entities or "",
            "sections": _sections_canonical(doc.doc, resolver),
            "report": str(doc.report_dir),
        }

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        resolver = self._resolver(
            data["reportId"], PurePosixPath(report_dir) if report_dir else PurePosixPath()
        )
        tags = data.get("_tags", [])
        return {
            **_remote_plain_canonical(data, tags),
            "affected_entities": _affected_entities_from_html(data.get("affectedEntities") or ""),
            "sections": _remote_sections_canonical(data, resolver),
            # Report membership must be part of the hash, exactly like
            # BsPageAdapter includes book/chapter (grison/adapters/bs_pages/):
            # without it, a cross-report MOVE with byte-identical content makes
            # `_apply_move`'s `needs_write` compare equal and the finding keeps
            # its OLD reportId on Ghostwriter forever, silently — the bug this
            # field closes. `report_dir` is None only for a reportId the index
            # no longer knows (orphaned data); falling back to "" still makes a
            # real reparent (known dir -> unknown) compare unequal.
            "report": report_dir or "",
        }

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        from grison.markdown.converter import html_to_md

        report_dir = path.parent
        resolver = self._resolver(data["reportId"], report_dir)
        cwe, plain = _split_tags(data.get("_tags", []))
        doc = finding_fmt.FindingDoc(
            severity=Severity.from_gw_id(data["severityId"]),
            finding_type=FindingType.from_gw_id(data["findingTypeId"]),
            cvss=finding_fmt.FindingCvss(vector=data["cvssVector"])
            if data.get("cvssVector")
            else None,
            cwe=cwe,
            tags=plain,
            title=_title_of(data),
            affected_entities=_affected_entities_from_html(data.get("affectedEntities") or "")
            or None,
            # headings=True: see _remote_sections_canonical's comment above.
            **{f: html_to_md(data.get(f) or "", headings=True, refs=resolver) for f in _SECTIONS},
        )
        return finding_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        from grison.sinks.file_sink import slugify

        base = PurePosixPath(report_dir) if report_dir else PurePosixPath("findings", "reports")
        return base / f"{slugify(_title_of(data))}.md"

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        report_dir = self.index.path_of(IndexKind.GW_REPORT, data["reportId"])
        if report_dir is None:
            return current
        return PurePosixPath(report_dir) / current.name

    def _end_of_band_position(self, ctx: GWContext, report_id: int, severity_id: int) -> int:
        siblings = [
            r
            for r in ctx.client.fetch_reported_findings()
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
        # omitted column untouched, which is exactly "preserved on update" (package
        # docstring); a cross-report MOVE still reparents via `reportId` above.
        row = ctx.client.update_reported_finding(id, fields)
        ctx.client.set_tags(id, "reportedFinding", _tags_for(doc.doc))
        tags = ctx.client.fetch_tags_for("reportedFinding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def delete(self, ctx: GWContext, id: int) -> None:
        ctx.client.delete_reported_finding(id)

    def restore(self, ctx: GWContext, preimage: Any) -> RemoteRecord:
        """Same two-way shape as :meth:`~grison.adapters.gw_findings.library.
        GwLibraryFindingAdapter.restore`: in-place update when the id still exists
        (undoing a PUSH/MOVE_EDIT — including a cross-report move, since
        ``reportId`` is part of the pre-image), recreate when it is gone (undoing
        a DELETE_REMOTE)."""
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
        return (
            f'"{title or "(untitled)"}" (finding {rid})'
            if rid is not None
            else f'"{title or "(untitled)"}"'
        )
