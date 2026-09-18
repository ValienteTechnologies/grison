"""Ghostwriter report directories + narrative sections, on the sync engine
(ENGINE.md Adapter protocol; the rework's brief, reports task).

Two things live here, deliberately NOT the same mechanism:

- **Report directories** (kind ``gw.report``) are a read-only STRUCTURE, exactly
  like :mod:`grison.adapters.bs_structure`'s books/chapters: created on pull
  (``slug(title)``, de-duplicated, never renamed — D3/D4), never created or deleted
  locally, never routed through :mod:`grison.engine.classify`/``apply`` themselves.
  :func:`sync_report_dirs` also (re)generates each report's two read-only mirrors,
  ``.report.yml`` and ``project.md``, via the shared
  :func:`grison.engine.mirrors.write_mirror_guarded` guard, and raises the
  missing-scope trip-wire (a project with zero scopes) as an ATTENTION event.
- **Narrative sections** (kind ``gw.reportSection``, one file per instance-defined
  ``extraFieldSpec`` row on the Report model) ARE a normal, real
  :class:`~grison.engine.adapter.Adapter`, routed through the ONE engine exactly
  like :class:`grison.adapters.bs_pages.BsPageAdapter` — push/pull/collision per
  section, the same classification table, the same pre-write re-fetch guard.

A section has no id of its own in Ghostwriter (``report.extraFields`` is a single
jsonb map on the report row, not a table with rows) — :func:`section_id` gives every
``(report, field)`` pair a distinct, decodable synthetic int id for
``.grison/index.json``/``.grison/state/`` (see its own docstring for the encoding and
why this is a safe, narrow, local choice rather than a data-model fork).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWReportContext, IndexRefResolver, slugify
from grison.engine.adapter import AdapterMode
from grison.engine.mirrors import MirrorWrite, write_mirror_guarded
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.formats import mirrors as mirrors_fmt
from grison.formats import narrative as narrative_fmt
from grison.index import Index, IndexKind
from grison.markdown.converter import ConverterError, html_to_md, md_to_html

NARRATIVE_DIR = "narrative"
REPORT_META = ".report.yml"
PROJECT_CONTEXT_FILE = "project.md"
_MIRROR_HEADER = "<!-- grison: regenerated every sync — do not edit -->\n\n"

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


# --- report directories + mirrors (structure-style, not the generic engine loop) ---


@dataclass
class ReportDirResult:
    created: list[str] = field(default_factory=list)
    materialized: list[str] = field(default_factory=list)
    scope_failures: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _report_dir_name(index: Index, report_id: int, title: str) -> str | None:
    existing = index.path_of(IndexKind.GW_REPORT, report_id)
    if existing is not None:
        return PurePosixPath(existing).name
    taken = {
        PurePosixPath(p).name for p, rec in index.records.items() if rec.kind is IndexKind.GW_REPORT
    }
    base = slugify(title)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def sync_report_dirs(
    root: Path, ctx: GWReportContext, index: Index, state: StateStore, snapshot: Snapshot,
    *, dry_run: bool = False, on_event: Callable[[str], None] | None = None,
) -> ReportDirResult:
    """Create any missing report directory for a report ``ctx.reports`` (fetched once
    per sync — the same heavy nested query that already supplies ``project.md``'s
    data) doesn't have one for yet, then (re)generate ``.report.yml``/``project.md``
    for every report. Call BEFORE the narrative/notes engine passes — they resolve a
    report's directory purely through ``ctx.dir_by_report_id``/``report_id_by_dir``,
    which the CLI refreshes from ``index`` right after this returns.

    Reports are Ghostwriter-owned (grison never creates/deletes one) — a report
    directory is a read-only structure exactly like
    :mod:`grison.adapters.bs_structure`'s books/chapters, never routed through the
    generic classify/apply engine loop.
    """
    del snapshot  # a report directory is only ever created here, never deleted through
    # grison — nothing to record for undo (mirrors BookUndoAdapter's own note: a
    # create-only structure kind that undo never needs to reverse via this pass).
    result = ReportDirResult()
    order = [s["internalName"] for s in ctx.field_specs]
    mirrors = state.load_mirrors()

    for rec in ctx.reports:
        rid = rec["id"]
        name = _report_dir_name(index, rid, rec.get("title") or f"report-{rid}")
        rel_dir = f"findings/reports/{name}"
        was_indexed = index.path_of(IndexKind.GW_REPORT, rid) is not None
        if not was_indexed:
            if dry_run:
                result.created.append(rel_dir)
                if on_event:
                    on_event(f"would create {rel_dir}")
                continue
            index.set(rel_dir, IndexKind.GW_REPORT, rid)
            (root / rel_dir).mkdir(parents=True, exist_ok=True)
            result.created.append(rel_dir)
            if on_event:
                on_event(f"create {rel_dir}")
        if dry_run:
            continue

        project = rec.get("project") or {}
        meta_doc = mirrors_fmt.ReportMetaDoc(
            title=rec.get("title") or "",
            project=mirrors_fmt.ProjectMeta(
                id=project.get("id"),
                client=mirrors_fmt.ClientMeta(
                    id=(project.get("client") or {}).get("id"),
                    name=(project.get("client") or {}).get("name"),
                    short_name=(project.get("client") or {}).get("shortName"),
                ),
                start_date=project.get("startDate"), end_date=project.get("endDate"),
            ),
            status=mirrors_fmt.StatusMeta(
                complete=rec.get("complete"), archived=rec.get("archived"),
                delivered=rec.get("delivered"),
            ),
            dates=mirrors_fmt.DatesMeta(creation=rec.get("creation"),
                                        last_update=rec.get("last_update")),
            narrative_order=order,
        )
        meta_text = mirrors_fmt.dump_report_meta(meta_doc)
        _write_mirror(root, f"{rel_dir}/{REPORT_META}", meta_text, mirrors, result,
                      dry_run=dry_run, on_event=on_event)

        if project.get("id") is not None:
            _check_scope(rid, project, result)
            ctx_text = project_context_to_md(project)
            _write_mirror(root, f"{rel_dir}/{PROJECT_CONTEXT_FILE}", ctx_text, mirrors, result,
                          dry_run=dry_run, on_event=on_event)

    if not dry_run:
        state.save_mirrors(mirrors)
    return result


def _write_mirror(
    root: Path, rel_path: str, text: str, mirrors: dict[str, str], result: ReportDirResult,
    *, dry_run: bool, on_event: Callable[[str], None] | None,
) -> None:
    outcome = write_mirror_guarded(root, rel_path, text, mirrors, dry_run=dry_run)
    if outcome.outcome is MirrorWrite.UNCHANGED:
        return
    if outcome.outcome is MirrorWrite.HAND_EDITED:
        assert outcome.message is not None
        result.skipped.append((rel_path, outcome.message))
        if on_event:
            on_event(f"skip {rel_path}: {outcome.message}")
        return
    result.materialized.append(rel_path)
    if on_event:
        verb = "would mirror" if outcome.outcome is MirrorWrite.WOULD_WRITE else "mirror"
        on_event(f"{verb} {rel_path}")


def _check_scope(report_id: int, project: dict[str, Any], result: ReportDirResult) -> None:
    """Missing-scope trip-wire (the rework's brief, reports task): a project with
    zero ``scopes`` rows is reported (ATTENTION — exit 1) but never blocks any other
    report's sync."""
    if not project.get("scopes"):
        codename = (project.get("codename") or "").strip()
        result.scope_failures.append(
            f"report {report_id} ({codename}): project has no scope defined"
        )


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


# --- narrative sections: a real engine Adapter -------------------------------------


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


class NarrativeSectionAdapter:
    kind = "gw.reportSection"
    mode: AdapterMode = "read-write"

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
                    path=rel, doc=SectionDoc(field=md.stem, body=doc.body, report_dir=rdir.name),
                    raw_text=raw,
                )

    def _spec_by_name(self, ctx: GWReportContext) -> dict[str, dict[str, Any]]:
        return {s["internalName"]: s for s in ctx.field_specs}

    def _resolver_for(self, ctx: GWReportContext, report_dir: str) -> IndexRefResolver:
        return IndexRefResolver(index=ctx.index, report_dir=report_dir)

    def _section_data(
        self, ctx: GWReportContext, report_id: int, spec: dict[str, Any], html: str,
        report_dir: str,
    ) -> dict[str, Any]:
        losses: list[str] = []
        try:
            body_md = html_to_md(
                html or "", headings=True, refs=self._resolver_for(ctx, report_dir),
                on_loss=losses.append,
            ).strip()
        except ConverterError:
            body_md = html or ""
        return {
            "report_id": report_id, "spec_id": spec["id"], "field": spec["internalName"],
            "report_dir": report_dir, "body_md": body_md, "losses": losses,
        }

    def fetch_remote(self, ctx: GWReportContext) -> dict[int, RemoteRecord]:
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
                    id=section_id(rid, spec["id"]), data=data, losses=data["losses"],
                )
        return out

    def refetch(self, ctx: GWReportContext, id: int) -> RemoteRecord | None:
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
        return {"body": doc.body.strip()}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        return {"body": data["body_md"]}

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        return narrative_fmt.dump(narrative_fmt.NarrativeDoc(body=data["body_md"]))

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        return PurePosixPath("findings", "reports", data["report_dir"], NARRATIVE_DIR,
                             f"{data['field']}.md")

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        del data
        return current  # a report is never renamed/reparented (D+ENGINE.md) — no relocation

    def _push(
        self, ctx: GWReportContext, report_id: int, field_name: str, doc: SectionDoc,
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
        html = md_to_html(doc.body.strip(), headings=True, refs=self._resolver_for(ctx, rdir))
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
        self, ctx: GWReportContext, report_id: int, doc: SectionDoc,
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
        self._push(ctx, report_id, spec["internalName"],
                  SectionDoc(field=spec["internalName"], body="", report_dir=""))

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
