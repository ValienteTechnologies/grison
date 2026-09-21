"""Narrative sections (kind ``gw.reportSection``) — a real engine
:class:`~grison.engine.adapter.Adapter`, one file per instance-defined
``extraFieldSpec`` row on the Report model.

See :mod:`grison.adapters.gw_report` (this package's ``__init__``) for the
module-level overview of report directories vs. narrative sections, including
why a section has no id of its own in Ghostwriter and why :func:`section_id`
exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWReportContext, IndexRefResolver
from grison.engine.adapter import AdapterMode
from grison.engine.filesets import canonical_prose, canonical_remote_prose
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.formats import narrative as narrative_fmt
from grison.index import Index
from grison.markdown.converter import ConverterError, html_to_md, md_to_html

NARRATIVE_DIR = "narrative"

#: Multiplies a report id up to make room for its field-spec ids underneath — see
#: :func:`section_id`. extraFieldSpec ids are small, per-install Hasura serial ids;
#: 100k of headroom per report is generous and keeps the encoding trivial to decode.
_SECTION_ID_MULTIPLIER = 100_000


def section_id(report_id: int, spec_id: int) -> int:
    """The synthetic ``gw.reportSection`` identity for one report's one field.
    Chosen over e.g. a Cantor pairing for simplicity — see the module docstring."""
    if spec_id >= _SECTION_ID_MULTIPLIER:  # pragma: no cover — real installs are far below this
        raise ValueError(f"extraFieldSpec id {spec_id} too large for the section-id encoding")
    return report_id * _SECTION_ID_MULTIPLIER + spec_id


def decode_section_id(id: int) -> tuple[int, int]:
    """Inverse of :func:`section_id`: ``(report_id, spec_id)``."""
    return id // _SECTION_ID_MULTIPLIER, id % _SECTION_ID_MULTIPLIER


@dataclass(frozen=True)
class SectionDoc:
    """One local ``narrative/<field>.md`` file's parsed content, plus the
    directory-derived report it belongs to (D4's own pattern, mirrored from
    :class:`grison.adapters.bs_pages.PageDoc`: identity context attached at scan
    time, since the ``Adapter`` protocol's ``create``/``update`` never receive a
    path)."""

    field: str
    body: str
    report_dir: str


@dataclass
class NarrativeSectionAdapter:
    """Every indexed report's narrative sections, as ONE adapter instance per sync
    run.

    ``canonical_local`` (the ``Adapter`` protocol's ctx-less method — see
    ``ENGINE.md``'s Adapter protocol sketch) folds each embed's CURRENT remote
    id into the hash via :func:`grison.engine.filesets.canonical_prose`, exactly
    like :mod:`grison.adapters.gw_findings` does for findings' LOCAL side (D1: a
    reupload changes an evidence row's id under an unchanged path — see that
    module's docstring). ``canonical_remote`` folds the LITERAL id already
    present in the remote's own stored HTML instead, via :func:`grison.engine.
    filesets.canonical_remote_prose` — never re-derived through the same live
    index — so a reupload (which never touches this section's OWN stored HTML)
    leaves ``canonical_remote``'s payload unchanged while ``canonical_local``'s
    does change, and the section is re-PUSHed with the new id in the SAME run
    that did the reupload (D1: "replacing an image's bytes must re-push every
    finding referencing it, automatically") instead of a silent CLEAN or an
    unresolved-reference PULL overwrite. A captioned embed's caption/title never
    enters either hash (both helpers exclude it), so a caption round-trips
    exactly (see :class:`~grison.adapters._gw_common.IndexRefResolver`) without
    ever making the section look locally edited. ``canonical_local`` needs the
    CURRENT ``.grison/index.json`` (D3), which — unlike
    :class:`~grison.adapters.gw_findings.GwReportedFindingAdapter`, whose ``index``
    is a constructor field the CLI already threads through — this adapter has no
    such constructor-injected access to. ``grison.cli._run_reports_phase`` DOES
    construct it with one field, ``evidence_by_report`` (the report phase's own
    evidence-file-set sync result, shared with the findings phase's
    ``GwReportedFindingAdapter`` too — see that field's own docstring); the OTHER
    2 construction sites (offline status, ``grison undo``) still use a bare,
    zero-argument ``NarrativeSectionAdapter()``. Either way, ``self._index`` is
    captured as a side effect of
    ``fetch_remote``/``refetch`` — the only two methods this adapter has that DO
    receive ``ctx`` — which :mod:`grison.engine.documents`'s loop order guarantees
    run before any ``canonical_local``/``canonical_remote`` call on the SAME
    adapter instance within one sync (``scan_local``+``fetch_remote`` both
    happen up front, then classification). The one caller with no ctx-bearing
    call at all, ever (:func:`grison.engine.offline_status.
    compute_offline_status`), degrades to an empty index/no evidence lookup —
    the same "no id known" a library finding with no evidence at all already
    gets from :mod:`grison.adapters.gw_findings.common`'s own ``_EMPTY_RESOLVER``,
    never a crash."""

    kind = "gw.reportSection"
    mode: AdapterMode = "read-write"
    #: Every indexed report's evidence rows, built ONCE by ``grison.cli``'s reports
    #: phase (right after that phase's own evidence-file-set sync — see
    #: ``grison.cli._run_reports_phase``'s docstring) and shared with the findings
    #: phase's ``GwReportedFindingAdapter``, rather than each adapter re-fetching
    #: the same org-wide evidence list. ``None`` (every OTHER, zero-argument
    #: construction site: offline status, ``grison undo``) falls back to this
    #: adapter's own lazy, per-report ``ctx.evidence_for_report`` fetch — see
    #: :meth:`_evidence_lookup_for`.
    evidence_by_report: dict[int, dict[int, dict[str, Any]]] | None = None
    _index: Index = field(
        default_factory=lambda: Index(root=Path()), init=False, repr=False, compare=False
    )
    #: A per-report evidence-rows lookup, captured during ``fetch_remote``/
    #: ``refetch`` so ``canonical_remote`` can resolve a legacy ``{{.friendlyName}}``
    #: dot-form embed's name to an id (:class:`~grison.engine.filesets.
    #: _LiteralEmbedResolver`'s ``name_to_id`` fallback) without needing ``ctx``
    #: itself, which it never receives — see :meth:`_evidence_lookup_for`. ``None``
    #: (never called) degrades to no name resolution at all — the same safe
    #: "unresolved" floor an empty index already gives ``to_remote_id``.
    _evidence_lookup: Callable[[int], dict[int, dict[str, Any]]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def _evidence_lookup_for(
        self,
        ctx: GWReportContext,
    ) -> Callable[[int], dict[int, dict[str, Any]]]:
        """``self.evidence_by_report`` (pre-built, shared — see that field's own
        docstring) when given, else ``ctx.evidence_for_report`` (this adapter's own
        lazy, per-run-cached org-wide fetch — unaffected by phase ordering, just not
        shared with the findings phase)."""
        if self.evidence_by_report is not None:
            by_report = self.evidence_by_report
            return lambda report_id: by_report.get(report_id, {})
        return ctx.evidence_for_report

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "findings" / "reports"
        if not base.is_dir():
            return
        for rdir in sorted(p for p in base.iterdir() if p.is_dir()):
            ndir = rdir / NARRATIVE_DIR
            if not ndir.is_dir():
                continue
            for md in sorted(ndir.glob("*.md")):
                if md.name.endswith(".remote.md"):
                    continue
                rel = PurePosixPath(md.relative_to(root).as_posix())
                raw = md.read_text(encoding="utf-8")
                doc = narrative_fmt.parse(raw, path=md)
                yield LocalDoc(
                    path=rel,
                    doc=SectionDoc(field=md.stem, body=doc.body, report_dir=rdir.name),
                    raw_text=raw,
                )

    def _spec_by_name(self, ctx: GWReportContext) -> dict[str, dict[str, Any]]:
        return {s["internalName"]: s for s in ctx.field_specs}

    def _resolver_for(
        self, ctx: GWReportContext, report_id: int, report_dir: str
    ) -> IndexRefResolver:
        """The caption-carrying resolver (D1) — ``evidence_rows`` comes from
        :meth:`_evidence_lookup_for` (passed LAZILY: see
        :class:`~grison.adapters._gw_common.IndexRefResolver`'s own field docstring
        — a report with no evidence reference anywhere never triggers the org-wide
        evidence fetch) so a pulled embed's caption/description are the evidence
        row's OWN current values (:class:`~grison.adapters._gw_common.
        IndexRefResolver`'s ``to_local``), never dropped. Uses ``self.evidence_by_report``
        (pre-built and shared, when given — see that field's docstring) exactly like
        :meth:`canonical_remote` does, rather than a second, independent org-wide
        fetch of the same rows."""
        lookup = self._evidence_lookup_for(ctx)
        return IndexRefResolver(
            index=ctx.index, report_dir=report_dir, evidence_rows=lambda: lookup(report_id)
        )

    def _canon_resolver(self, report_dir: str) -> IndexRefResolver:
        """The id-folding-only resolver :meth:`canonical_local`/:meth:`canonical_remote`
        use (see the class docstring on why they can't just call :meth:`_resolver_for`
        — no ``ctx`` reaches either of them). No ``evidence_rows`` needed: neither
        caller reads caption/description through it — :func:`grison.engine.filesets.
        canonical_prose` only ever calls ``to_remote_id``."""
        return IndexRefResolver(index=self._index, report_dir=report_dir)

    def _section_data(
        self,
        ctx: GWReportContext,
        report_id: int,
        spec: dict[str, Any],
        html: str,
        report_dir: str,
    ) -> dict[str, Any]:
        losses: list[str] = []
        try:
            body_md = html_to_md(
                html or "",
                headings=True,
                refs=self._resolver_for(ctx, report_id, report_dir),
                on_loss=losses.append,
            ).strip()
        except ConverterError:
            body_md = html or ""
        return {
            "report_id": report_id,
            "spec_id": spec["id"],
            "field": spec["internalName"],
            "report_dir": report_dir,
            "body_md": body_md,
            "losses": losses,
            # the RAW html, kept alongside the rendered body_md (above) so
            # canonical_remote can fold the LITERAL embed id (see class
            # docstring) instead of re-deriving one through body_md, which was
            # already produced by a (possibly-failed) live-index resolution.
            "html": html or "",
        }

    def fetch_remote(self, ctx: GWReportContext) -> dict[int, RemoteRecord]:
        self._index = ctx.index
        self._evidence_lookup = self._evidence_lookup_for(ctx)
        out: dict[int, RemoteRecord] = {}
        for rec in ctx.reports:
            rid = rec["id"]
            rdir = ctx.dir_by_report_id.get(rid)
            if rdir is None:
                continue  # not yet indexed as a report directory — sync_report_dirs runs first
            raw_extra = rec.get("extraFields") or {}
            for spec in ctx.field_specs:
                html = raw_extra.get(spec["internalName"]) or ""
                data = self._section_data(ctx, rid, spec, html, rdir)
                out[section_id(rid, spec["id"])] = RemoteRecord(
                    id=section_id(rid, spec["id"]),
                    data=data,
                    losses=data["losses"],
                )
        return out

    def refetch(self, ctx: GWReportContext, id: int) -> RemoteRecord | None:
        self._index = ctx.index
        self._evidence_lookup = self._evidence_lookup_for(ctx)
        report_id, spec_id = decode_section_id(id)
        row = ctx.client.fetch_report_by_pk(report_id)
        if row is None:
            return None
        spec = next((s for s in ctx.field_specs if s["id"] == spec_id), None)
        if spec is None:
            return None
        rdir = ctx.dir_by_report_id.get(report_id)
        if rdir is None:
            return None
        html = (row.get("extraFields") or {}).get(spec["internalName"]) or ""
        data = self._section_data(ctx, report_id, spec, html, rdir)
        return RemoteRecord(id=id, data=data, losses=data["losses"])

    def canonical_local(self, doc: SectionDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        return canonical_prose(doc.body.strip(), self._canon_resolver(doc.report_dir))

    def _name_to_id(self, report_id: int) -> dict[str, int]:
        if self._evidence_lookup is None:
            return {}
        rows = self._evidence_lookup(report_id)
        return {row["friendly_name"]: eid for eid, row in rows.items() if row.get("friendly_name")}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        return canonical_remote_prose(
            data["html"],
            headings=True,
            name_to_id=lambda: self._name_to_id(data["report_id"]),
        )

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        return narrative_fmt.dump(narrative_fmt.NarrativeDoc(body=data["body_md"]))

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        return PurePosixPath(
            "findings", "reports", data["report_dir"], NARRATIVE_DIR, f"{data['field']}.md"
        )

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        del data
        return current  # a report is never renamed/reparented (D+ENGINE.md) — no relocation

    def _push(
        self,
        ctx: GWReportContext,
        report_id: int,
        field_name: str,
        doc: SectionDoc,
    ) -> dict[str, Any]:
        """Merge one field's new HTML over a FRESH re-fetch of the report's whole
        ``extraFields`` map (the old ``_guard_stale_push`` behaviour, now scoped to
        this one adapter method — ENGINE.md's own pre-write re-fetch guard already
        re-fetched this SAME field once, immediately before calling this, to detect
        drift; this second, equally tight ``report_by_pk`` call is what actually
        supplies the other fields' current values so a push here never clobbers a
        concurrent edit to a field this run isn't touching)."""
        rdir = ctx.dir_by_report_id.get(report_id)
        if rdir is None:
            raise LookupError(f"report {report_id} has no known local directory")
        row = ctx.client.fetch_report_by_pk(report_id)
        if row is None:
            raise LookupError(f"report {report_id} no longer exists remotely")
        html = md_to_html(
            doc.body.strip(), headings=True, refs=self._resolver_for(ctx, report_id, rdir)
        )
        merged = dict(row.get("extraFields") or {})
        merged[field_name] = html
        ctx.client.update_report(report_id, {"extraFields": merged})
        return merged

    def create(self, ctx: GWReportContext, doc: SectionDoc) -> RemoteRecord:
        # A section is never independently "created" on Ghostwriter (every spec
        # field always exists on every report, defaulting to "" — see the module
        # docstring's note on why a genuinely new local file here is a narrow,
        # documented bootstrapping-order edge case) — a local narrative/<field>.md
        # for a KNOWN field reaching CREATE means the record hadn't been indexed
        # yet even though a matching report already had; this pushes it exactly
        # like an update.
        report_id = self._require_report_id(ctx, doc)
        return self._push_and_record(ctx, report_id, doc)

    def _require_report_id(self, ctx: GWReportContext, doc: SectionDoc) -> int:
        rid = ctx.report_id_by_dir.get(doc.report_dir)
        if rid is None:
            raise LookupError(f"unknown report directory {doc.report_dir!r} — not indexed")
        return rid

    def update(self, ctx: GWReportContext, id: int, doc: SectionDoc) -> RemoteRecord:
        report_id, _spec_id = decode_section_id(id)
        return self._push_and_record(ctx, report_id, doc)

    def _push_and_record(
        self,
        ctx: GWReportContext,
        report_id: int,
        doc: SectionDoc,
    ) -> RemoteRecord:
        spec = self._spec_by_name(ctx).get(doc.field)
        if spec is None:
            raise LookupError(f"{doc.field!r} is not a Ghostwriter extraFieldSpec field")
        merged = self._push(ctx, report_id, doc.field, doc)
        rdir = ctx.dir_by_report_id[report_id]
        data = self._section_data(ctx, report_id, spec, merged[doc.field], rdir)
        return RemoteRecord(id=section_id(report_id, spec["id"]), data=data, losses=data["losses"])

    def delete(self, ctx: GWReportContext, id: int) -> None:
        """DELETE_REMOTE for a section (ENGINE.md's classification table: a local
        file deleted, unmodified relative to the last sync -> the remote record is
        deleted) has no literal counterpart — a report's extraFieldSpec field always
        exists, never as a row that can be dropped. The faithful equivalent, and the
        one consistent with every other read-write kind sharing this ONE engine
        (D6) rather than a narrative-specific special case, is clearing the field's
        content back to empty — exactly what a brand-new, never-populated field
        already looks like remotely."""
        report_id, spec_id = decode_section_id(id)
        spec = next((s for s in ctx.field_specs if s["id"] == spec_id), None)
        if spec is None:
            raise LookupError(f"no extraFieldSpec {spec_id} on this Ghostwriter instance")
        self._push(
            ctx,
            report_id,
            spec["internalName"],
            SectionDoc(field=spec["internalName"], body="", report_dir=""),
        )

    def restore(self, ctx: GWReportContext, preimage: Any) -> RemoteRecord:
        report_id = preimage.get("report_id")
        field_name = preimage.get("field")
        body_md = preimage.get("body_md", "")
        if report_id is None or field_name is None:
            raise ValueError("no report/field recorded in the pre-image — cannot restore")
        return self._push_and_record(
            ctx, report_id, SectionDoc(field=field_name, body=body_md, report_dir="")
        )

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        del local, remote
        return None

    def remote_label(self, data: Any) -> str:
        if not isinstance(data, dict):
            return str(data)
        return f'"{data.get("field")}" (report {data.get("report_id")})'
