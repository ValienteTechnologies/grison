"""Ghostwriter project notes, append-only, on the sync engine (ENGINE.md Adapter
protocol; the rework's brief, reports task B).

A note (Ghostwriter's ``projectNote`` — exposed on ``project.comments``, per the
live schema) belongs to a PROJECT, not a report, but the workspace layout keeps it
under a report directory's ``notes/`` (D3/index.json can only key one path per
identity, so a project shared by several reports gets its notes filed under its
lowest-id report, deterministically — see :func:`_primary_report_dir`; a single-
report project, the common case, has no ambiguity at all).

Mode ``append-only`` (ENGINE.md/classify.py): a NEW local ``notes/<name>.md`` (no
frontmatter) is CREATEd once and then replaced, at the SAME path, by its mirrored
read-only form; an existing mirrored note is always CLEAN from this engine's own
point of view — never pushed, updated, or deleted again. Undo of a create deletes
the note (the only remote write this adapter ever performs going forward); ``update``/
``restore`` are structurally unreachable for an append-only kind (classify.py never
returns PUSH/DELETE_REMOTE for one) and raise rather than pretending to work.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWReportContext, IndexRefResolver
from grison.engine.adapter import AdapterMode
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.formats import note as note_fmt
from grison.formats.common import FormatError
from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.sinks.file_sink import slugify

NOTES_DIR = "notes"


@dataclass(frozen=True)
class NoteLocalDoc:
    body: str
    report_dir: str


def _primary_report_dir(ctx: GWReportContext, project_id: int | None) -> tuple[int, str] | None:
    """The lowest-id report of ``project_id`` that already has a known local
    directory — the deterministic single home a project's notes are filed under
    when its project has more than one report (see module docstring)."""
    if project_id is None:
        return None
    candidates = sorted(rid for rid, pid in ctx.project_id_by_report.items() if pid == project_id)
    for rid in candidates:
        rdir = ctx.dir_by_report_id.get(rid)
        if rdir is not None:
            return rid, rdir
    return None


class ReportNoteAdapter:
    kind = "gw.projectNote"
    mode: AdapterMode = "append-only"

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "findings" / "reports"
        if not base.is_dir():
            return
        for rdir in sorted(p for p in base.iterdir() if p.is_dir()):
            ndir = rdir / NOTES_DIR
            if not ndir.is_dir():
                continue
            for md in sorted(ndir.glob("*.md")):
                if md.name.endswith(".remote.md"):
                    continue
                rel = PurePosixPath(md.relative_to(root).as_posix())
                raw = md.read_text(encoding="utf-8")
                doc: NoteLocalDoc | None
                try:
                    parsed = note_fmt.parse(raw, path=md)
                    doc = NoteLocalDoc(body=parsed.body, report_dir=rdir.name)
                except FormatError:
                    doc = None
                yield LocalDoc(path=rel, doc=doc, raw_text=raw)

    def _note_data(
        self,
        ctx: GWReportContext,
        row: dict[str, Any],
        *,
        report_id: int,
        report_dir: str,
    ) -> dict[str, Any]:
        user = row.get("user") or {}
        losses: list[str] = []
        try:
            body_md = html_to_md(
                row.get("note") or "",
                headings=True,
                refs=IndexRefResolver(index=ctx.index, report_dir=report_dir),
                on_loss=losses.append,
            ).strip()
        except ConverterError:
            body_md = row.get("note") or ""
        return {
            "id": row.get("id"),
            "project_id": row.get("projectId"),
            "report_id": report_id,
            "report_dir": report_dir,
            "body_md": body_md,
            "losses": losses,
            "author": (user.get("name") or user.get("username") or "").strip(),
            "timestamp": row.get("timestamp"),
        }

    def fetch_remote(self, ctx: GWReportContext) -> dict[int, RemoteRecord]:
        out: dict[int, RemoteRecord] = {}
        for rec in sorted(ctx.reports, key=lambda r: r["id"]):
            rid = rec["id"]
            rdir = ctx.dir_by_report_id.get(rid)
            if rdir is None:
                continue
            project = rec.get("project") or {}
            for note in project.get("comments") or []:
                nid = note.get("id")
                if nid is None or nid in out:
                    continue  # already attached to an earlier (lower-id) report of this project
                data = self._note_data(
                    ctx, dict(note, projectId=project.get("id")), report_id=rid, report_dir=rdir
                )
                out[nid] = RemoteRecord(id=nid, data=data, losses=data["losses"])
        return out

    def refetch(self, ctx: GWReportContext, id: int) -> RemoteRecord | None:
        row = ctx.client.fetch_project_note_by_pk(id)
        if row is None:
            return None
        home = _primary_report_dir(ctx, row.get("projectId"))
        if home is None:
            return None
        report_id, report_dir = home
        data = self._note_data(ctx, row, report_id=report_id, report_dir=report_dir)
        return RemoteRecord(id=id, data=data, losses=data["losses"])

    def canonical_local(self, doc: NoteLocalDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        return {"body": doc.body.strip()}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        return {"body": data["body_md"]}

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        doc = note_fmt.NoteDoc(
            author=data.get("author") or None,
            timestamp=data.get("timestamp"),
            body=data["body_md"],
            has_frontmatter=True,
        )
        return note_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        words = data["body_md"].split()
        slug = slugify(" ".join(words[:6])) if words else "note"
        return PurePosixPath("findings", "reports", data["report_dir"], NOTES_DIR, f"{slug}.md")

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        del data
        return current  # a note's home report is fixed once assigned — never reparented

    def create(self, ctx: GWReportContext, doc: NoteLocalDoc) -> RemoteRecord:
        report_id = ctx.report_id_by_dir.get(doc.report_dir)
        if report_id is None:
            raise LookupError(f"unknown report directory {doc.report_dir!r} — not indexed")
        project_id = ctx.project_id_by_report.get(report_id)
        if project_id is None:
            raise LookupError(f"report {report_id} has no project — cannot push a note")
        operator_id, _username = ctx.resolve_operator()
        html = md_to_html(
            doc.body.strip(),
            headings=True,
            refs=IndexRefResolver(index=ctx.index, report_dir=doc.report_dir),
        )
        note_id = ctx.client.insert_project_note(project_id, html, operator_id, date.today())
        row = ctx.client.fetch_project_note_by_pk(note_id)
        if row is None:  # pragma: no cover — defensive: the mutation just returned this id
            row = {
                "id": note_id,
                "projectId": project_id,
                "note": html,
                "timestamp": None,
                "user": None,
            }
        data = self._note_data(ctx, row, report_id=report_id, report_dir=doc.report_dir)
        return RemoteRecord(id=note_id, data=data, losses=data["losses"])

    def update(self, ctx: GWReportContext, id: int, doc: NoteLocalDoc) -> RemoteRecord:
        raise AssertionError(
            "gw.projectNote is append-only — classify.py never returns PUSH for an "
            "append-only kind, so this is never called"
        )

    def delete(self, ctx: GWReportContext, id: int) -> None:
        """Only ever called to undo a note create grison itself just pushed — an
        existing, previously-synced note is never deleted (append-only)."""
        ctx.client.delete_project_note(id)

    def restore(self, ctx: GWReportContext, preimage: Any) -> RemoteRecord:
        raise AssertionError(
            "gw.projectNote's undo is create-only (see CreateUndoAdapter) — restore "
            "is only called for a push/move_edit/delete_remote op, none of which this "
            "adapter ever records"
        )

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        del local, remote
        return None

    def remote_label(self, data: Any) -> str:
        if not isinstance(data, dict):
            return str(data)
        excerpt = (data.get("body_md") or "").strip().splitlines()[:1]
        text = excerpt[0][:40] if excerpt else "(empty)"
        return f'"{text}" (note {data.get("id")})'
