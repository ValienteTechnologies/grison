from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.model import Event, Plan
from grison.engine.state import StateStore
from grison.fsio import atomic_write_bytes
from grison.hashing import digest
from grison.index import Index, IndexKind

from .model import FileSetAdapter, _canonical, _event, _hash_bytes


def _apply_delete_local(
    root: Path,
    p: Plan,
    index: Index,
    state: StateStore,
    events: list[Event],
    dry: bool,
) -> None:
    """ "deleted remotely" (D1: the folder mirrors the remote set both ways — a row
    that disappeared on the server removes the local mirror file too), reachable
    when a file's remote row is gone but the local copy still matches the last
    synced base (classify.py's ordinary DELETE_LOCAL row)."""
    assert p.path is not None and p.id is not None
    if dry:
        events.append(_event("delete-local", path=p.path, dry_run=True))
        return
    (root / p.path).unlink(missing_ok=True)
    index.remove(str(p.path))
    state.forget(p.kind, p.id)
    events.append(_event("delete-local", path=p.path))


def _apply_pull(
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    index: Index,
    state: StateStore,
    events: list[Event],
    dry: bool,
) -> None:
    assert p.remote is not None and p.path is not None
    row = p.remote
    path = p.path
    label = adapter.remote_label(row.data)
    is_new = index.get(str(path)) is None
    if dry:
        events.append(_event("pull", path=path, label=label if is_new else None, dry_run=True))
        return
    body = adapter.fetch_body(ctx, row.id)
    atomic_write_bytes(root / path, body)
    index.set(str(path), IndexKind(adapter.kind), row.id)
    body_hash = _hash_bytes(body)
    state.put(
        adapter.kind,
        row.id,
        base=digest(
            _canonical(
                body_hash=body_hash,
                caption=row.data.get("caption", ""),
                description=row.data.get("description", ""),
                supports_caption=adapter.supports_caption,
            )
        ),
        witness={**row.witness, "body_hash": body_hash},
    )
    events.append(_event("pull", path=path, label=label if is_new else None))
