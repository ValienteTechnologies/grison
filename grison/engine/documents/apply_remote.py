from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.events import build_event, emit_losses
from grison.engine.model import Event, Outcome, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot, UndoOp
from grison.fsio import atomic_write_text
from grison.hashing import digest
from grison.index import Index, IndexKind

from .guards import _refetch_guard, _write_collision_sidecar
from .model import RunOptions


def _apply_create(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: Adapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    dry: bool,
) -> None:
    assert p.local is not None and p.path is not None
    if dry:
        events.append(Event(verb="create", path=str(p.path), dry_run=True))
        return
    # item 11, fix-fin1: a force-local "resurrect" of a remote-deleted record
    # (classify.py converts DELETE_LOCAL -> CREATE) reaches here with `p.id`
    # still set to the OLD, now-gone remote id — the same Plan the classify
    # loop built for the original present-indexed slot, only its outcome
    # changed. An ordinary CREATE (a brand-new, never-indexed local file) never
    # sets `p.id` at all, so this is unambiguous.
    old_id = p.id
    rec = adapter.create(ctx, p.local.doc)
    # crash-ordering (ENGINE.md §7): the index entry is written the instant the server
    # returns an id, before ANY other bookkeeping — a crash right after this line can
    # never produce a duplicate create on the next sync (see tests/test_engine_apply.py).
    index.set(str(p.path), IndexKind(adapter.kind), rec.id)
    if old_id is not None and old_id != rec.id:
        state.forget(adapter.kind, old_id)
    # local_preimage: the author's own pre-create bytes — undoing this create restores
    # them (grison.engine.undo._replay_one), rather than deleting the file or leaving
    # it in its post-create mirrored form.
    snapshot.record(
        UndoOp(
            kind=adapter.kind,
            outcome="create",
            path=str(p.path),
            id=rec.id,
            local_preimage=p.local.raw_text,
        )
    )
    text = adapter.render_local(rec.data, path=p.path)
    if text != p.local.raw_text:
        atomic_write_text(root / p.path, text)
    state.put(
        adapter.kind, rec.id, base=digest(adapter.canonical_remote(rec.data)), witness=rec.witness
    )
    emit_losses(events, str(p.path), rec.losses)
    events.append(Event(verb="create", path=str(p.path)))


def _apply_update(  # noqa: PLR0913
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
    assert p.local is not None and p.id is not None and p.path is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, options)
    if drifted:
        p.outcome = Outcome.COLLISION
        if dry:
            # ENGINE.md §9: dry run performs everything except writes of any kind —
            # a drift discovered here must report the same "would collide" event a
            # real run would, without writing the sidecar (item 3, fix-fin1: this
            # branch used to write it unconditionally, before the `dry` check below
            # it, unlike _apply_collision's own equivalent branch).
            events.append(Event(verb="collision", path=str(p.path), dry_run=True))
            return
        _write_collision_sidecar(root, adapter, p, fresh)
        events.append(
            Event(
                verb="collision",
                path=str(p.path),
                detail="changed on the server since classification",
            )
        )
        return
    # the wysiwyg/draft/recycle-bin guard is re-checked here, against the FRESH
    # pre-write data, not just the bulk-fetch snapshot classify() used — a page can
    # flip editor/draft state between classification and this write.
    veto = adapter.veto(p.local.doc if p.local else None, fresh.data if fresh else None)
    if veto is not None:
        p.outcome = Outcome.SKIP
        p.reason = veto.reason
        p.severity = veto.severity
        events.append(build_event("skip", p, adapter, detail=veto.reason))
        return
    if dry:
        verb = "move" if p.outcome is Outcome.MOVE_EDIT else "push"
        detail = f"from {p.move_from}" if p.move_from else ""
        events.append(Event(verb=verb, path=str(p.path), detail=detail, dry_run=True))
        return
    if fresh is None:
        # force-local on a "edited locally, deleted remotely" collision (ENGINE.md /
        # BRIEF: "re-create remotely") — the id classify() knew is gone; a PUSH here
        # means CREATE a new record and re-point the index at it, not update() an id
        # that no longer exists.
        rec = adapter.create(ctx, p.local.doc)
        old_id = p.id
        if p.move_from is not None:
            # a MOVE_EDIT whose old id is gone: the old path's entry still names
            # that dead id — drop it, or the next run reports a phantom record at
            # the old path and FORGETs it one cycle late.
            index.remove(str(p.move_from))
        index.set(str(p.path), IndexKind(adapter.kind), rec.id)
        state.forget(adapter.kind, old_id)
        snapshot.record(
            UndoOp(
                kind=adapter.kind,
                outcome="create",
                path=str(p.path),
                id=rec.id,
                local_preimage=p.local.raw_text,
            )
        )
        text = adapter.render_local(rec.data, path=p.path)
        if text != p.local.raw_text:
            atomic_write_text(root / p.path, text)
        state.put(
            adapter.kind,
            rec.id,
            base=digest(adapter.canonical_remote(rec.data)),
            witness=rec.witness,
        )
        emit_losses(events, str(p.path), rec.losses)
        events.append(Event(verb="push", path=str(p.path), detail="re-created remotely"))
        return
    preimage = fresh.data
    # The LOCAL half of a push undo (item 3, fix-findings): NOT `p.local.raw_text`
    # (that's the author's ABOUT-TO-BE-PUSHED text — the edit undo is supposed to
    # revert, not what it should restore) — the file as it would read if it
    # mirrored the OLD, pre-push remote state, exactly like a genuine pull of
    # `preimage` would have written it. Computed once, here, rather than lazily
    # at replay time, so undo never needs to re-derive a rendering decision the
    # forward loop already made once (and the undo module stays adapter-agnostic
    # about whether ANY given "push" op even has local content to restore — a
    # file-set caption push's `local_preimage` stays `None`, on purpose: a
    # caption/description change never touches the evidence file's own bytes).
    local_preimage = adapter.render_local(preimage, path=p.path)
    op = UndoOp(
        kind=adapter.kind,
        outcome=p.outcome.value,
        path=str(p.path),
        id=p.id,
        remote_preimage=preimage,
        move_from=str(p.move_from) if p.move_from else None,
        local_preimage=local_preimage,
    )
    snapshot.record(op)
    resp = adapter.update(ctx, p.id, p.local.doc)
    if p.move_from is not None:
        index.move(str(p.move_from), str(p.path))
    text = adapter.render_local(resp.data, path=p.path)
    if text != p.local.raw_text:
        atomic_write_text(root / p.path, text)
    # ENGINE.md §6's canonicalisation-after-push hash IS the post-write canonical
    # hash a "push"/"move_edit" undo's re-fetch guard compares against (item 4) —
    # one computation, two uses, so the two can never quietly disagree.
    resp_hash = digest(adapter.canonical_remote(resp.data))
    op.post_write_hash = resp_hash
    state.put(adapter.kind, p.id, base=resp_hash, witness=resp.witness)
    emit_losses(events, str(p.path), resp.losses)
    verb = "move" if p.outcome is Outcome.MOVE_EDIT else "push"
    detail = f"from {p.move_from}" if p.move_from else ""
    events.append(Event(verb=verb, path=str(p.path), detail=detail))


def _apply_move(  # noqa: PLR0913
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
    """Identical content: index-only bookkeeping UNLESS the directory move implies a
    different remote parent — in which case exactly one write happens (never a no-op
    PUT for a plain rename)."""
    assert p.local is not None and p.id is not None and p.move_from is not None
    remote = p.remote
    needs_write = remote is not None and (
        adapter.canonical_local(p.local.doc) != adapter.canonical_remote(remote.data)
    )
    if options.dry_run:
        events.append(
            Event(verb="move", path=str(p.path), detail=f"from {p.move_from}", dry_run=True)
        )
        return
    index.move(str(p.move_from), str(p.path))
    if needs_write:
        p.outcome = Outcome.MOVE_EDIT  # reuse the update path for the reparent write
        _apply_update(root, ctx, adapter, p, index, state, snapshot, events, options)
        return
    events.append(Event(verb="move", path=str(p.path), detail=f"from {p.move_from}"))


def _apply_delete_remote(  # noqa: PLR0913
    ctx: Any,
    adapter: Adapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
) -> None:
    assert p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, options)
    if drifted:
        p.outcome = Outcome.COLLISION
        events.append(
            Event(
                verb="collision",
                path=str(p.path),
                detail="changed on the server since classification",
            )
        )
        return
    if dry:
        events.append(Event(verb="delete-remote", path=str(p.path), dry_run=True))
        return
    preimage = fresh.data if fresh is not None else None
    snapshot.record(
        UndoOp(
            kind=adapter.kind,
            outcome="delete_remote",
            path=str(p.path),
            id=p.id,
            remote_preimage=preimage,
        )
    )
    adapter.delete(ctx, p.id)
    if p.path is not None:
        index.remove(str(p.path))
    state.forget(adapter.kind, p.id)
    events.append(Event(verb="delete-remote", path=str(p.path)))
