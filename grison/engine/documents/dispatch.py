from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.common import apply_delete_local
from grison.engine.events import build_event
from grison.engine.model import Event, Outcome, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.hashing import digest
from grison.index import Index

from .apply_local import _apply_pull
from .apply_remote import _apply_create, _apply_delete_remote, _apply_move, _apply_update
from .guards import _write_collision_sidecar
from .model import RunOptions


def _apply_one(  # noqa: PLR0912, PLR0913, PLR0915
    root: Path,
    ctx: Any,
    adapter: Adapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
) -> None:
    try:
        _dispatch(root, ctx, adapter, p, index, state, snapshot, events, options)
    except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5)
        p.outcome = Outcome.FAILED
        p.reason = f"{type(e).__name__}: {e}"
        events.append(build_event("failed", p, adapter, detail=p.reason))


def _dispatch(  # noqa: PLR0912, PLR0913, PLR0915
    root: Path,
    ctx: Any,
    adapter: Adapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
) -> None:
    kind = adapter.kind
    dry = options.dry_run

    if p.outcome is Outcome.CLEAN:
        return

    if p.outcome is Outcome.REPAIR:
        if not dry and p.id is not None:
            wit = state.get(kind, p.id)
            local_hash = digest(adapter.canonical_local(p.local.doc)) if p.local else None
            state.put(kind, p.id, base=local_hash, witness=wit.witness if wit else {})
        events.append(Event(verb="repair", path=str(p.path), dry_run=dry))
        return

    if p.outcome is Outcome.FORGET:
        if not dry and p.path is not None and p.id is not None:
            index.remove(str(p.path))
            state.forget(kind, p.id)
        events.append(Event(verb="forget", path=str(p.path), dry_run=dry))
        return

    if p.outcome is Outcome.INVALID:
        events.append(Event(verb="invalid", path=str(p.path), detail=", ".join(p.rule_ids)))
        return

    if p.outcome is Outcome.WITHHELD:
        events.append(Event(verb="withheld", path=str(p.path) if p.path else None))
        return

    if p.outcome is Outcome.SKIP:
        events.append(build_event("skip", p, adapter, detail=p.reason))
        return

    if p.outcome is Outcome.CREATE:
        _apply_create(root, ctx, adapter, p, index, state, snapshot, events, dry)
        return

    if p.outcome in (Outcome.PUSH, Outcome.MOVE_EDIT):
        _apply_update(root, ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome is Outcome.MOVE:
        _apply_move(root, ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome in (Outcome.PULL, Outcome.PULL_NEW):
        _apply_pull(root, adapter, p, index, state, snapshot, events, dry)
        return

    if p.outcome is Outcome.DELETE_REMOTE:
        _apply_delete_remote(ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome is Outcome.DELETE_LOCAL:
        apply_delete_local(root, p, index, state, events, dry)
        return

    if p.outcome is Outcome.COLLISION:
        # ENGINE.md §8: a COLLISION writes the remote version next to the file —
        # gated on `dry` (mirrors grison.engine.filesets.dispatch's equivalent
        # classify-time-collision branch): dry run reports the same event but
        # performs no write of any kind (ENGINE.md §9).
        if not dry:
            _write_collision_sidecar(root, adapter, p, p.remote)
        events.append(Event(verb="collision", path=str(p.path), dry_run=dry))
        return
