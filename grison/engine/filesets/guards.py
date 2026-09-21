from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.common import refetch_guard
from grison.engine.model import Event, Outcome, Plan, RemoteRecord
from grison.engine.sidecar import write_sidecar as _write_sidecar
from grison.engine.state import StateStore
from grison.hashing import digest

from .model import FileSetAdapter, RunOptions, _cached_body_hash, _canonical, _event, _hash_bytes


def _refetch_guard(
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    state: StateStore,
    options: RunOptions,
) -> tuple[RemoteRecord | None, bool]:
    """The file-set binding of :func:`grison.engine.common.refetch_guard` — same
    pre-write drift check every remote-destructive write (re-upload, caption push,
    delete-remote) must run immediately before its write, not just the raw
    ``fresh is None`` check these used to do. Bytes can never silently change
    under an id (D1: a body change always mints a new id — see
    ``_apply_reupload``), so the fresh comparison only needs the row's current
    caption/description; the body-hash half of the canonical digest is the one
    already cached from classification (:func:`_cached_body_hash`), reused as-is
    rather than re-downloaded — EXCEPT on a genuine cache miss (state lost, or a
    dry-run classify that deliberately never warmed it — see `_classify_one`/
    `_classify_missing`), where defaulting to ``""`` would never equal the real
    canonical hash `Plan.remote.cached_hash` carries and would false-COLLIDE
    every single time; a real download is the only correct fallback there."""
    if p.id is None:
        return None, False
    rid = p.id
    cached = _cached_body_hash(state, adapter.kind, rid)
    body_hash = cached if cached is not None else _hash_bytes(adapter.fetch_body(ctx, rid))
    forced = p.path is not None and p.path in options.force_local
    return refetch_guard(
        refetch=lambda: adapter.refetch(ctx, rid),
        expected_hash=p.remote.cached_hash if p.remote is not None else None,
        canonical_hash=lambda fresh: digest(
            _canonical(
                body_hash=body_hash,
                caption=fresh.data.get("caption", ""),
                description=fresh.data.get("description", ""),
                supports_caption=adapter.supports_caption,
            )
        ),
        forced=forced,
    )


def _write_collision_sidecar(
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    path: PurePosixPath | None,
    remote: RemoteRecord | None,
) -> None:
    """The file-set binding of :func:`grison.engine.sidecar.write_sidecar` (item 5,
    same shared write :mod:`grison.engine.documents` uses for its own,
    text-shaped records): the remote version's bytes come from
    :meth:`FileSetAdapter.fetch_body`, never a document render — never a document:
    the scaffolded ``.gitignore``'s ``*.remote.*`` entry already covers it (unlike
    the document engine, a file-set sidecar's extension is whatever the shadowed
    file's is, not a fixed ``.md``, so a literal ``.remote.md`` pattern would have
    missed it — see :func:`~grison.engine.sidecar.is_sidecar_name`)."""
    if path is None or remote is None:
        return
    _write_sidecar(root, path, remote, lambda: adapter.fetch_body(ctx, remote.id))


def _collide(
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    p: Plan,
    fresh: RemoteRecord | None,
    events: list[Event],
    dry: bool,
) -> None:
    """The ONE drift->COLLISION branch every remote-destructive write's pre-write
    re-fetch guard takes on drift (re-upload, caption push, delete-remote — item 2,
    dedup round 2, this used to be copied three times verbatim in
    :mod:`.apply_remote`): mark the plan COLLISION, write the sidecar (unless
    ``dry`` — ENGINE.md §9: dry run performs everything except writes of any kind),
    and emit the same "changed on the server since classification" event."""
    p.outcome = Outcome.COLLISION
    if not dry:
        _write_collision_sidecar(root, ctx, adapter, p.path, fresh)
    events.append(
        _event("collision", path=p.path, detail="changed on the server since classification")
    )


def _preimage(adapter: FileSetAdapter, ctx: Any, row: RemoteRecord) -> dict[str, Any]:
    """The full ``row.data`` (adapter-specific — e.g. ``gw.evidence``'s ``reportId``,
    needed so ``restore()`` re-uploads into the SAME scope regardless of which
    scope-bound adapter instance ``grison undo`` happens to be replaying through:
    ``grison.engine.undo.replay``'s adapter map is keyed by bare kind string, one
    entry for every report/book, so a fileset adapter's ``restore`` must recover its
    own scope from the preimage, never from ``self``) plus the base64 body (D7:
    "deletes keep the bytes in the snapshot")."""
    body = adapter.fetch_body(ctx, row.id)
    return {**row.data, "body_b64": base64.b64encode(body).decode("ascii")}


def undo_restore(adapter: FileSetAdapter, ctx: Any, preimage: dict[str, Any]) -> RemoteRecord:
    """The shared body every file-set adapter's own ``restore`` delegates to —
    decode the snapshot's base64 body and re-upload it (D1: "undo (deletes keep
    the bytes in the snapshot)")."""
    body = base64.b64decode(preimage["body_b64"])
    return adapter.upload(
        ctx,
        filename=preimage["filename"],
        body=body,
        caption=preimage.get("caption", ""),
        description=preimage.get("description", ""),
    )
