"""The ONE apply loop (ENGINE.md 'The apply loop') for one adapter (one record kind)
per call. :func:`run` is what ``grison sync`` calls once per engine-managed kind (today
just ``bs.page``, ``bs.book``, ``bs.chapter``, ``bs.shelf`` — the wiki step); later
steps call it again for their own adapters, unchanged.

Order: classify (identity pairing first, then the table) -> validation gate ->
change guard -> apply (pre-write re-fetch guard, undo capture, per-record isolation) ->
bookkeeping (index + state, atomic per record) -> canonicalisation-after-push ->
collision sidecars.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.common import (
    change_guard,
    indexed_for_kind,
    partition_present,
    run_apply_loop,
    validation_gate,
)
from grison.engine.model import Event, KindSummary, LocalDoc, Outcome, Plan, RemoteRecord
from grison.engine.sidecar import clear_stale_sidecars
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index
from grison.validator.registry import Failure

from .classify import _classify_matched, _classify_missing, _pairing_plans
from .dispatch import _apply_one
from .model import RunOptions


def run(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: Adapter,
    *,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    failures: list[Failure],
    options: RunOptions | None = None,
) -> tuple[list[Plan], list[Event], KindSummary]:
    """Run the full classify+apply cycle for ``adapter``'s kind. Mutates ``index``
    in place (callers save it once after every adapter has run); ``snapshot`` collects
    undo ops across possibly-several adapter runs sharing one sync's snapshot dir.
    Returns the plans reached, the events emitted (in application order — callers
    render/print them, live or after the fact, via :mod:`grison.engine.events`), and
    this kind's summary."""
    options = options or RunOptions()
    kind = adapter.kind
    events: list[Event] = []
    summary = KindSummary(kind=kind)

    local_docs: dict[PurePosixPath, LocalDoc] = {d.path: d for d in adapter.scan_local(root)}
    remote_records: dict[int, RemoteRecord] = adapter.fetch_remote(ctx)
    indexed = indexed_for_kind(index, kind)
    indexed_ids = set(indexed.values())

    present_indexed, missing_paths, unindexed_paths = partition_present(local_docs, indexed)
    remote_unindexed = {i for i in remote_records if i not in indexed_ids}

    plans: list[Plan] = _pairing_plans(
        adapter,
        ctx,
        root,
        missing_paths,
        unindexed_paths,
        indexed,
        local_docs,
        remote_records,
        state,
    )
    paired_missing = {p.move_from for p in plans if p.move_from is not None}
    paired_unindexed = {p.path for p in plans if p.move_from is not None}

    for mpath in sorted(present_indexed, key=str):
        plans.append(
            _classify_matched(
                adapter, mpath, indexed[mpath], local_docs, remote_records, state, options
            )
        )
    for mpath in sorted(missing_paths - paired_missing, key=str):
        plans.append(
            _classify_missing(adapter, mpath, indexed[mpath], remote_records, state, options)
        )
    for mpath in sorted(unindexed_paths - paired_unindexed, key=str):
        plans.append(Plan(kind=kind, outcome=Outcome.CREATE, path=mpath, local=local_docs[mpath]))
    for rid in sorted(remote_unindexed):
        plans.append(Plan(kind=kind, outcome=Outcome.PULL_NEW, id=rid, remote=remote_records[rid]))

    # The validation gate (ENGINE.md 'Apply loop' item 1) — shared with the file-set
    # engine (grison.engine.common.validation_gate) — only ever turns a remote-write
    # outcome into INVALID; a plan it flagged is excluded from the veto check below,
    # exactly as it would be by the single combined loop this used to be (an INVALID
    # plan is never also SKIP-by-veto).
    validation_gate(plans, failures)
    # veto (a server-side condition the table can't express — documents only, no
    # file-set equivalent) only ever turns a remote-write outcome into SKIP.
    for p in plans:
        if p.outcome in (Outcome.CLEAN, Outcome.REPAIR, Outcome.FORGET, Outcome.INVALID):
            continue
        if p.local is not None or p.remote is not None:
            veto = adapter.veto(
                p.local.doc if p.local else None, p.remote.data if p.remote else None
            )
            if veto is not None:
                p.outcome = Outcome.SKIP
                p.reason = veto.reason
                p.severity = veto.severity

    change_guard(plans, options)

    def _apply(p: Plan) -> None:
        _apply_one(root, ctx, adapter, p, index, state, snapshot, events, options)

    def _label(p: Plan) -> str:
        return (
            str(p.path)
            if p.path is not None
            else (adapter.remote_label(p.remote.data) if p.remote is not None else str(p.id))
        )

    run_apply_loop(plans, summary, _apply, _label)

    if not options.dry_run:
        clear_stale_sidecars(root, plans)
    return plans, events, summary
