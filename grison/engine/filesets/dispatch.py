from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.model import Event, Outcome, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index

from .apply_local import _apply_delete_local, _apply_pull
from .apply_remote import _apply_caption_push, _apply_create, _apply_delete_remote, _apply_reupload
from .captions import ReferenceCaption
from .guards import _write_collision_sidecar
from .model import FileSetAdapter, RunOptions, _event, _hash_bytes, _record_base


def _apply_one(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
    local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    try:
        _dispatch(
            root, ctx, adapter, p, index, state, snapshot, events, options, local_files, captions
        )
    except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5)
        p.outcome = Outcome.FAILED
        p.reason = f"{type(e).__name__}: {e}"
        events.append(
            _event(
                "failed",
                path=p.path,
                label=adapter.remote_label(p.remote.data) if p.remote else None,
                detail=p.reason,
            )
        )


def _dispatch(  # noqa: PLR0911, PLR0912, PLR0913
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
    local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    kind = adapter.kind
    dry = options.dry_run

    if p.outcome is Outcome.CLEAN:
        return
    if p.outcome is Outcome.INVALID:
        # The validation gate (item 2, fix-findings — ENGINE.md 'Apply loop'
        # item 1, mirrored from grison.engine.documents.dispatch._dispatch): a file
        # `sync_fileset`'s own caller already found `grison validate` failures
        # for is never uploaded — surfaced naming the rule id(s), never
        # silently dropped.
        events.append(_event("invalid", path=p.path, detail=", ".join(p.rule_ids)))
        return
    if p.outcome is Outcome.FAILED:
        # a pre-built failure (today: a caption conflict `collect_captions`
        # degraded rather than raised — see `sync_fileset`) that never went
        # through classification/apply at all; just surface it.
        events.append(_event("failed", path=p.path, detail=p.reason))
        return
    if p.outcome is Outcome.REPAIR:
        # L == R already (that's what REPAIR means) — restamp base to the
        # CANONICAL hash both sides already agree on (body+caption+description,
        # same shape every other base in this module uses), not a bare body
        # hash: a mismatched format here would never equal either side's
        # canonical hash again, so every future sync would see base != L and
        # re-REPAIR forever instead of the "no-op from here on" REPAIR promises.
        if not dry and p.id is not None and p.path is not None and p.remote is not None:
            body = local_files.get(p.path.name)
            if body is not None:
                body_hash = _hash_bytes(body)
                st = state.get(kind, p.id)
                _record_base(
                    state,
                    kind,
                    p.id,
                    body_hash=body_hash,
                    caption=p.remote.data.get("caption", ""),
                    description=p.remote.data.get("description", ""),
                    supports_caption=adapter.supports_caption,
                    witness=st.witness if st else {},
                )
        events.append(_event("repair", path=p.path))
        return
    if p.outcome is Outcome.WITHHELD:
        events.append(_event("withheld", path=p.path))
        return
    if p.outcome is Outcome.MOVE:
        assert p.move_from is not None
        if dry:
            events.append(_event("move", path=p.path, detail=f"from {p.move_from}", dry_run=True))
            return
        index.move(str(p.move_from), str(p.path))
        events.append(_event("move", path=p.path, detail=f"from {p.move_from}"))
        return
    if p.outcome is Outcome.FORGET:
        if not dry and p.path is not None and p.id is not None:
            index.remove(str(p.path))
            state.forget(kind, p.id)
        events.append(_event("forget", path=p.path, dry_run=dry))
        return
    if p.outcome is Outcome.DELETE_LOCAL:
        _apply_delete_local(root, p, index, state, events, dry)
        return
    if p.outcome is Outcome.CREATE:
        _apply_create(ctx, adapter, p, index, state, snapshot, events, dry, local_files, captions)
        return
    if p.outcome is Outcome.MOVE_EDIT:
        _apply_reupload(
            root, ctx, adapter, p, index, state, snapshot, events, options, local_files, captions
        )
        return
    if p.outcome is Outcome.PUSH:
        _apply_caption_push(root, ctx, adapter, p, state, snapshot, events, options, captions)
        return
    if p.outcome in (Outcome.PULL, Outcome.PULL_NEW):
        _apply_pull(root, ctx, adapter, p, index, state, events, dry)
        return
    if p.outcome is Outcome.DELETE_REMOTE:
        _apply_delete_remote(root, ctx, adapter, p, index, state, snapshot, events, options)
        return
    if p.outcome is Outcome.COLLISION:
        # ENGINE.md §8: a COLLISION writes the remote version next to the file —
        # gated on `dry` (mirrors grison.engine.documents.dispatch._dispatch's own
        # COLLISION branch, the equivalent classify-time-collision branch for
        # documents): dry run reports the same event but performs no write of any
        # kind (ENGINE.md §9).
        if not dry:
            _write_collision_sidecar(root, ctx, adapter, p.path, p.remote)
        events.append(_event("collision", path=p.path, dry_run=dry))
        return
