"""JSON-payload and last-sync bookkeeping helpers shared by the CLI's commands —
see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeVar

from grison.cli.phases.findings import FindingsPhaseResult
from grison.cli.phases.reports import ReportsPhaseResult
from grison.cli.phases.wiki import WikiPhaseResult
from grison.engine import events as engine_events
from grison.engine.model import Event, KindSummary
from grison.engine.offline_status import StatusEntry
from grison.engine.state import StateStore


def _kinds_json(summaries: dict[str, KindSummary]) -> dict[str, Any]:
    """The ``{"kinds": {...}}`` shape every phase's ``--json`` payload and
    last-sync summary shares: one entry per kind, its counts + problem paths."""
    return {k: {"counts": s.counts, "problem_paths": s.problem_paths} for k, s in summaries.items()}


def _remote_phase_json(
    remote_summaries: dict[str, dict[str, KindSummary]], phase: str, error: str | None
) -> dict[str, Any] | str:
    """One ``--remote`` phase's ``--json`` payload: the per-kind counts/problem
    paths this phase's dry run actually reached, or — when that leg's credentials
    were missing/it failed before reaching this phase — the error message (``str``,
    same shape offline ``status``'s ``remote_error`` used before this phase split)."""
    if phase not in remote_summaries:
        return error if error is not None else "not attempted"
    return {"kinds": _kinds_json(remote_summaries[phase])}


def _entry_json(e: StatusEntry) -> dict[str, Any]:
    return {
        "path": str(e.path),
        "bucket": e.bucket,
        "reasons": list(e.reasons),
        "moved_from": str(e.moved_from) if e.moved_from is not None else None,
    }


class _HasFileCounts(Protocol):
    """The shape :func:`_fileset_json`/:func:`_fileset_status_line` need from a
    file-set counts object (``grison.cli.commands.status._FilesetCounts``) —
    a ``Protocol`` here, rather than importing that dataclass directly, keeps
    ``payloads`` from depending on ``commands.status`` (which itself depends on
    ``payloads``). Read-only properties, not plain attributes: ``_FilesetCounts``
    is a frozen dataclass, whose fields structurally satisfy a read-only property
    but not a settable one."""

    @property
    def files(self) -> int: ...

    @property
    def collisions(self) -> int: ...


def _fileset_json(c: _HasFileCounts) -> dict[str, int]:
    return {"files": c.files, "collisions": c.collisions}


def _fileset_status_line(counts: Mapping[PurePosixPath, _HasFileCounts]) -> str:
    parts = []
    for d, c in sorted(counts.items()):
        part = f"{d} {c.files}"
        if c.collisions:
            part += f" ({c.collisions} collision-sidecar(s) pending)"
        parts.append(part)
    return ", ".join(parts)


def _phase_error(phase_errors: list[str], name: str) -> str | None:
    prefix = f"{name} sync failed: "
    return next((m[len(prefix) :] for m in phase_errors if m.startswith(prefix)), None)


_PhaseResult = TypeVar("_PhaseResult")


def _phase_payload_or_error(
    result: _PhaseResult | None,
    payload_fn: Callable[[_PhaseResult], dict[str, Any]],
    phase_errors: list[str],
    name: str,
) -> dict[str, Any] | None:
    """One phase's slot in ``sync --json``'s combined document (item 8, fix-fin1):
    the phase's normal ``{"events": [...], "summary": {...}}`` payload when it
    produced a result, ``{"error": "..."}`` when it raised before producing one,
    or ``null`` when it never even ran at all (e.g. the wiki phase with no
    BookStack credentials configured) — the three states a phase can be in,
    never conflated."""
    if result is not None:
        return payload_fn(result)
    error = _phase_error(phase_errors, name)
    return {"error": error} if error is not None else None


def _phase_payload(
    summaries: dict[str, KindSummary],
    events: list[Event],
    exit_code: int,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One phase's contribution to ``sync --json``'s ONE combined document (item 8,
    fix-fin1) — ``events``/``summary`` only; ``snapshot``/the run's overall
    ``exit_code`` are hoisted to the top level by the caller (the SAME snapshot dir
    and overall exit-code decision are shared by every phase this run, so repeating
    them per-phase was pure redundancy, not independent information). ``extra``
    (e.g. the wiki phase's own ``structure``, the reports phase's own ``dirs``) is
    merged into ``summary`` alongside ``kinds``/``exit_code`` — every phase's own
    ``_*_payload`` wrapper supplies its own, or none."""
    summary: dict[str, Any] = {"kinds": _kinds_json(summaries)}
    if extra:
        summary.update(extra)
    summary["exit_code"] = exit_code
    return {"events": [engine_events.event_dict(e) for e in events], "summary": summary}


def _phase_last_sync_summary(
    summaries: dict[str, KindSummary],
    snapshot_dir: Path | None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One phase's ``.grison/state/last-sync.json`` entry body — ``kinds`` +
    ``snapshot_dir``, plus whatever phase-specific ``extra`` (the reports phase's
    own ``dirs`` counts) its own wrapper supplies."""
    out: dict[str, Any] = {"kinds": _kinds_json(summaries)}
    if extra:
        out.update(extra)
    out["snapshot_dir"] = str(snapshot_dir) if snapshot_dir else None
    return out


def _findings_payload(findings: FindingsPhaseResult) -> dict[str, Any]:
    return _phase_payload(findings.summaries, findings.events, findings.exit_code)


def _findings_last_sync_summary(findings: FindingsPhaseResult) -> dict[str, Any]:
    return _phase_last_sync_summary(findings.summaries, findings.snapshot_dir)


def _wiki_payload(wiki: WikiPhaseResult) -> dict[str, Any]:
    return _phase_payload(
        wiki.summaries,
        wiki.events,
        wiki.exit_code,
        extra={
            "structure": {
                "created_books": wiki.structure.created_books,
                "created_chapters": wiki.structure.created_chapters,
                "materialized": wiki.structure.materialized,
                "skipped": wiki.structure.skipped,
                "errors": wiki.structure.errors,
            }
        },
    )


def _wiki_last_sync_summary(wiki: WikiPhaseResult) -> dict[str, Any]:
    return _phase_last_sync_summary(wiki.summaries, wiki.snapshot_dir)


def _reports_payload(reports: ReportsPhaseResult) -> dict[str, Any]:
    return _phase_payload(
        reports.summaries,
        reports.events,
        reports.exit_code,
        extra={
            "dirs": {
                "created": reports.dirs.created,
                "materialized": reports.dirs.materialized,
                "scope_failures": reports.dirs.scope_failures,
                "skipped": reports.dirs.skipped,
                "errors": reports.dirs.errors,
            }
        },
    )


def _reports_last_sync_summary(rep: ReportsPhaseResult) -> dict[str, Any]:
    return _phase_last_sync_summary(
        rep.summaries,
        rep.snapshot_dir,
        extra={
            "dirs": {
                "created": rep.dirs.created,
                "materialized": rep.dirs.materialized,
                "scope_failures": len(rep.dirs.scope_failures),
                "errors": len(rep.dirs.errors),
            }
        },
    )


def _record_phase_last_sync(
    root: Path,
    phase: str,
    *,
    ok: bool,
    error: str | None,
    summary: dict[str, Any] | None,
) -> None:
    """``.grison/state/last-sync.json`` (ENGINE.md 'State') — read by ``grison
    status``: ``{"phases": {"findings": {...}, "report": {...}, "wiki": {...}}}``, one
    entry per phase that has ever run, each independently updated so one phase's
    outcome never clobbers another's. A failing phase (``error`` set, ``summary``
    ``None``) still gets an entry — `grison status`'s "last sync" line must never say
    a broken phase looks fine just because it never wrote anything."""
    state = StateStore(root)
    payload = state.load_last_sync() or {}
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        phases = {}
    entry: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "ok": ok}
    if error is not None:
        entry["error"] = error
    if summary is not None:
        entry["summary"] = summary
    phases[phase] = entry
    payload["phases"] = phases
    state.save_last_sync(payload)
