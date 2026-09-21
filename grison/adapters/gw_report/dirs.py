"""Report directories (kind ``gw.report``) + their two read-only mirrors.

See :mod:`grison.adapters.gw_report` (this package's ``__init__``) for the
module-level overview of report directories vs. narrative sections.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWReportContext, slugify
from grison.engine.mirrors import MirrorWrite, write_mirror_guarded
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.formats import mirrors as mirrors_fmt
from grison.index import Index, IndexKind

from .mirror import project_context_to_md

REPORT_META = ".report.yml"
PROJECT_CONTEXT_FILE = "project.md"


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
    root: Path,
    ctx: GWReportContext,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    *,
    dry_run: bool = False,
    on_event: Callable[[str], None] | None = None,
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
                start_date=project.get("startDate"),
                end_date=project.get("endDate"),
            ),
            status=mirrors_fmt.StatusMeta(
                complete=rec.get("complete"),
                archived=rec.get("archived"),
                delivered=rec.get("delivered"),
            ),
            dates=mirrors_fmt.DatesMeta(
                creation=rec.get("creation"), last_update=rec.get("last_update")
            ),
            narrative_order=order,
        )
        meta_text = mirrors_fmt.dump_report_meta(meta_doc)
        _write_mirror(
            root,
            f"{rel_dir}/{REPORT_META}",
            meta_text,
            mirrors,
            result,
            dry_run=dry_run,
            on_event=on_event,
        )

        if project.get("id") is not None:
            _check_scope(rid, project, result)
            ctx_text = project_context_to_md(project)
            _write_mirror(
                root,
                f"{rel_dir}/{PROJECT_CONTEXT_FILE}",
                ctx_text,
                mirrors,
                result,
                dry_run=dry_run,
                on_event=on_event,
            )

    if not dry_run:
        state.save_mirrors(mirrors)
    return result


def _write_mirror(
    root: Path,
    rel_path: str,
    text: str,
    mirrors: dict[str, str],
    result: ReportDirResult,
    *,
    dry_run: bool,
    on_event: Callable[[str], None] | None,
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
