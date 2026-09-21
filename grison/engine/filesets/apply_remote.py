from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.model import Event, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot, UndoOp
from grison.hashing import digest
from grison.index import Index, IndexKind

from .captions import ReferenceCaption
from .guards import _collide, _preimage, _refetch_guard
from .model import (
    FileSetAdapter,
    RunOptions,
    _event,
    _hash_bytes,
    _record_base,
    caption_only_canonical,
)


def _apply_create(  # noqa: PLR0913
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    dry: bool,
    local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    assert p.path is not None
    if dry:
        events.append(_event("create", path=p.path, dry_run=True))
        return
    # item 11, fix-fin1: a force-local "resurrect" of a remote-deleted file
    # (classify.py converts DELETE_LOCAL -> CREATE) reaches here with `p.id`
    # still set to the OLD, now-gone remote id — see the mirrored comment in
    # grison.engine.documents.apply_remote._apply_create.
    old_id = p.id
    body = local_files[p.path.name]
    opinion = captions.get(p.path.name)
    caption = opinion.caption if opinion is not None and opinion.has_opinion else ""
    description = opinion.description if opinion is not None and opinion.has_opinion else ""
    row = adapter.upload(
        ctx, filename=p.path.name, body=body, caption=caption, description=description
    )
    index.set(str(p.path), IndexKind(adapter.kind), row.id)
    if old_id is not None and old_id != row.id:
        state.forget(adapter.kind, old_id)
    snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=row.id))
    body_hash = _hash_bytes(body)
    _record_base(
        state,
        adapter.kind,
        row.id,
        body_hash=body_hash,
        caption=row.data.get("caption", ""),
        description=row.data.get("description", ""),
        supports_caption=adapter.supports_caption,
        witness=row.witness,
    )
    p.remote = row  # so _rows_after()'s caption-rewrite pass sees this newly-created row too
    events.append(_event("create", path=p.path))


def _apply_reupload(  # noqa: PLR0913
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
    """Bytes changed under the same name (D1): a new remote row, the old one
    deleted, the index repointed at the new id — never an in-place update. If a
    local caption opinion also changed in this same sync, it rides along on the
    re-upload (a fresh row's caption is set once, at create time — see
    ``upload``'s contract — there is no separate metadata PUSH to follow up
    with, since the plan that would have carried it was replaced by this one).

    Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a caption/description change (or
    the row vanishing outright) on the server since classification becomes a
    COLLISION — nothing uploaded, nothing deleted — instead of silently
    re-uploading over a change grison never saw."""
    assert p.path is not None and p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted:
        _collide(root, ctx, adapter, p, fresh, events, dry)
        return
    if dry:
        events.append(
            _event("push", path=p.path, detail="bytes changed — new remote row", dry_run=True)
        )
        return
    body = local_files[p.path.name]
    opinion = captions.get(p.path.name)
    if opinion is not None and opinion.has_opinion:
        caption, description = opinion.caption, opinion.description
    else:
        caption = fresh.data.get("caption", "") if fresh else ""
        description = fresh.data.get("description", "") if fresh else ""
    new_row = adapter.upload(
        ctx, filename=p.path.name, body=body, caption=caption, description=description
    )
    old_id = p.id
    if fresh is not None:
        snapshot.record(
            UndoOp(
                kind=adapter.kind,
                outcome="delete_remote",
                path=str(p.path),
                id=old_id,
                remote_preimage=_preimage(adapter, ctx, fresh),
            )
        )
        adapter.delete(ctx, old_id)
    snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=new_row.id))
    index.set(str(p.path), IndexKind(adapter.kind), new_row.id)
    state.forget(adapter.kind, old_id)
    body_hash = _hash_bytes(body)
    _record_base(
        state,
        adapter.kind,
        new_row.id,
        body_hash=body_hash,
        caption=caption,
        description=description,
        supports_caption=adapter.supports_caption,
        witness=new_row.witness,
    )
    p.remote = new_row  # so _rows_after()/the caller's caption-rewrite pass sees the new row
    events.append(_event("push", path=p.path, detail="bytes changed — new remote row"))


def _apply_caption_push(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
    captions: dict[str, ReferenceCaption],
) -> None:
    """Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a caption/description that changed
    on the server since classification (or the row being gone outright) is a
    COLLISION with the same wording the document engine uses, not a
    push-specific "deleted on the server" message — never a metadata write over a
    change grison never saw."""
    assert p.id is not None and p.path is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted or fresh is None:
        # fresh is None only reachable here when force-local overrode drifted for a
        # row that's now gone entirely (refetch_guard's own "not forced" rule) — a
        # caption push has no "recreate" fallback the way a reupload/delete-remote
        # does (there is nothing to attach the caption to), so it collides too.
        _collide(root, ctx, adapter, p, fresh, events, dry)
        return
    if dry:
        events.append(_event("push", path=p.path, detail="caption/description", dry_run=True))
        return
    # Re-derive the winning local opinion exactly as _classify_one did (a local
    # opinion with no non-empty alt anywhere defers to the remote's own value,
    # which can never itself be the reason for a PUSH — see that function).
    opinion = captions.get(p.path.name)
    assert opinion is not None and opinion.has_opinion, (
        "a caption PUSH with no local opinion would mean local mirrored remote "
        "and could never have differed from base — classify() would not have "
        "produced PUSH for this record"
    )
    local_caption, local_description = opinion.caption, opinion.description
    op = UndoOp(
        kind=adapter.kind,
        outcome="push",
        path=str(p.path),
        id=p.id,
        remote_preimage=_preimage(adapter, ctx, fresh),
    )
    snapshot.record(op)
    updated = adapter.update_caption(
        ctx, p.id, caption=local_caption, description=local_description
    )
    # ENGINE.md §6's canonicalisation-after-push, extended to undo (item 4): the
    # post-write canonical hash a "push" undo re-fetch guard compares against —
    # see `caption_only_canonical`'s docstring for why bytes never enter it.
    op.post_write_hash = digest(caption_only_canonical(updated.data))
    # A caption/description push never touches bytes (D1) — reuse the cached
    # digest instead of re-downloading; fall back to a download only if this
    # id somehow has no cached digest yet.
    cached = state.get(adapter.kind, p.id)
    cached_hash = cached.witness.get("body_hash") if cached else None
    body_hash = (
        cached_hash if isinstance(cached_hash, str) else _hash_bytes(adapter.fetch_body(ctx, p.id))
    )
    _record_base(
        state,
        adapter.kind,
        p.id,
        body_hash=body_hash,
        caption=updated.data.get("caption", ""),
        description=updated.data.get("description", ""),
        supports_caption=True,
        witness=updated.witness,
    )
    p.remote = updated
    events.append(_event("push", path=p.path, detail="caption/description"))


def _apply_delete_remote(
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    options: RunOptions,
) -> None:
    """Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a row that changed on the server
    since classification is a COLLISION — nothing deleted — with the same wording
    the document engine uses, not a raw ``fresh is None`` check."""
    assert p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted:
        _collide(root, ctx, adapter, p, fresh, events, dry)
        return
    if dry:
        events.append(_event("delete-remote", path=p.path, dry_run=True))
        return
    if fresh is not None:
        snapshot.record(
            UndoOp(
                kind=adapter.kind,
                outcome="delete_remote",
                path=str(p.path),
                id=p.id,
                remote_preimage=_preimage(adapter, ctx, fresh),
            )
        )
        adapter.delete(ctx, p.id)
    if p.path is not None:
        index.remove(str(p.path))
    state.forget(adapter.kind, p.id)
    events.append(_event("delete-remote", path=p.path))
