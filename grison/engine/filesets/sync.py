from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.apply import REMOTE_WRITE_OUTCOMES
from grison.engine.apply import change_guard as _apply_change_guard
from grison.engine.model import Event, KindSummary, Outcome, Plan
from grison.engine.sidecar import clear_stale_sidecars as _clear_stale_sidecars
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index
from grison.validator.registry import Failure

from .captions import collect_captions
from .classify import _classify_missing, _classify_one, _dedupe_path, _pairing_plans, _rows_after
from .dispatch import _apply_one
from .model import FileSetAdapter, FileSetResult, RunOptions, _local_files


def sync_fileset(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    folder: PurePosixPath,
    *,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    doc_bodies: dict[PurePosixPath, str] | None = None,
    options: RunOptions | None = None,
    failures: list[Failure] | None = None,
) -> FileSetResult:
    """Sync one file set (one report's ``evidence/``, or one book's ``images/``).

    ``doc_bodies`` (only meaningful when ``adapter.supports_caption``) is every
    document in this file set's scope, already read — used to derive each
    file's local caption opinion (:func:`collect_captions`); omit/empty for an
    adapter with no caption concept at all.

    ``failures`` (item 2, fix-findings — ENGINE.md 'Apply loop' item 1, "a
    document with failures is never pushed or created", extended to a file-set
    record — the validation gate used to only ever run for ordinary documents,
    never for a file-set create/reupload/push, so a file `grison validate`
    flagged hard (``REF-008``, an evidence/image file's own bad name) still got
    synced to the remote server): the SAME failure list the caller already
    computed for this scope (report/book) via
    :func:`grison.validator.validate_workspace` — matched here by exact path,
    mirroring :func:`grison.engine.apply.run`'s identical gate. Omit/empty for
    a caller that hasn't validated (never silently skips the gate — an empty
    list just means nothing failed)."""
    options = options or RunOptions()
    failures = failures or []
    invalid_paths = {f.path for f in failures}
    kind = adapter.kind
    events: list[Event] = []
    summary = KindSummary(kind=kind)

    local_files = _local_files(root, folder)
    remote_rows = adapter.list_remote(ctx)
    captions, caption_conflicts = (
        collect_captions(doc_bodies or {}, folder=folder) if adapter.supports_caption else ({}, [])
    )

    indexed = {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind.value == kind and PurePosixPath(p).parent == folder
    }
    indexed_ids = set(indexed.values())
    name_by_id = {v: k.name for k, v in indexed.items()}

    present_names = set(local_files) & set(name_by_id.values())
    missing_names = set(name_by_id.values()) - set(local_files)
    unindexed_names = set(local_files) - set(name_by_id.values())
    remote_unindexed = set(remote_rows) - indexed_ids

    plans, paired_missing_names, paired_unindexed_names = _pairing_plans(
        kind,
        folder,
        missing_names,
        unindexed_names,
        indexed,
        state,
        local_files,
    )
    # a caption conflict (REF-004) that slipped past the validator degrades just
    # this one file to "no local caption opinion" (it has no entry in `captions`
    # above) and becomes its own FAILED record here — never an exception that
    # would abort every other file/finding in this sync (module docstring,
    # ENGINE.md §5 per-record isolation).
    for conflict in caption_conflicts:
        plans.append(
            Plan(
                kind=kind,
                outcome=Outcome.FAILED,
                path=folder / conflict.name,
                reason=conflict.detail,
            )
        )

    for name in sorted(present_names - paired_missing_names):
        path = folder / name
        rid = indexed[path]
        plans.append(
            _classify_one(
                adapter,
                ctx,
                path,
                rid,
                local_files[name],
                remote_rows,
                state,
                options,
                captions,
            )
        )
    for name in sorted(missing_names - paired_missing_names):
        path = folder / name
        rid = indexed[path]
        plans.append(_classify_missing(adapter, ctx, path, rid, remote_rows, state, options))
    for name in sorted(unindexed_names - paired_unindexed_names):
        plans.append(Plan(kind=kind, outcome=Outcome.CREATE, path=folder / name))
    # A running "already spoken for" set, not just the pre-sync disk/index snapshot
    # (`local_files`/`indexed`): two remote rows named identically in the SAME sync
    # (e.g. two Ghostwriter evidence uploads both called "shot.png") would otherwise
    # both call `_dedupe_path` against the same unchanged pre-sync state and both
    # land on "shot.png", the second silently clobbering the first on disk and in
    # the index with no event at all. Seeded from local_files/indexed exactly like
    # before, then extended after every dedupe so each subsequent PULL_NEW in this
    # same loop sees the names already claimed by its predecessors.
    claimed_names = set(local_files) | {p.name for p in indexed}
    for rid in sorted(remote_unindexed):
        row = remote_rows[rid]
        path = _dedupe_path(folder / row.data["filename"], claimed_names)
        claimed_names.add(path.name)
        plans.append(Plan(kind=kind, outcome=Outcome.PULL_NEW, id=rid, remote=row, path=path))

    # The validation gate (ENGINE.md 'Apply loop' item 1), mirrored verbatim
    # from grison.engine.apply.run: only ever turns a remote-write outcome into
    # INVALID — a pull/delete-local proceeds regardless, exactly like a
    # document's own gate.
    for p in plans:
        if p.outcome in REMOTE_WRITE_OUTCOMES and p.path is not None:
            if str(p.path) in invalid_paths:
                p.rule_ids = tuple(f.rule_id for f in failures if f.path == str(p.path))
                p.outcome = Outcome.INVALID

    _apply_change_guard(plans, options)

    for p in plans:
        _apply_one(
            root, ctx, adapter, p, index, state, snapshot, events, options, local_files, captions
        )
        summary.bump(p.outcome)
        if p.is_problem:
            label = (
                str(p.path)
                if p.path is not None
                else adapter.remote_label(p.remote.data if p.remote is not None else {})
            )
            summary.problem_paths.append(label)

    if not options.dry_run:
        _clear_stale_sidecars(root, plans)

    resolved: dict[str, tuple[str, str]] = {}
    if adapter.supports_caption:
        rows_after = _rows_after(remote_rows, plans)
        for name, data in rows_after.items():
            opinion = captions.get(name)
            if opinion is not None and opinion.has_opinion:
                resolved[name] = (opinion.caption, opinion.description)
            else:
                resolved[name] = (data.get("caption", ""), data.get("description", ""))

    return FileSetResult(plans=plans, events=events, summary=summary, resolved_captions=resolved)
