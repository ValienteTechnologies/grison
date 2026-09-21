from __future__ import annotations

from pathlib import Path
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.common import refetch_guard
from grison.engine.model import Plan, RemoteRecord
from grison.engine.sidecar import write_sidecar
from grison.hashing import digest

from .model import RunOptions, _remote_hash


def _refetch_guard(
    ctx: Any,
    adapter: Adapter,
    p: Plan,
    options: RunOptions,
) -> tuple[RemoteRecord | None, bool]:
    """The document-adapter binding of :func:`grison.engine.common.refetch_guard`:
    ``p.remote`` may be a skip-detail-fetch placeholder (a small sentinel dict,
    missing most fields — see the adapter's own ``fetch_remote``), which would
    never equal a freshly fetched full record even when nothing actually changed
    — ``_remote_hash`` already knows to read that placeholder's ``cached_hash``
    instead of hashing its sentinel content."""
    if p.id is None:
        return None, False
    rid = p.id
    forced = p.path is not None and p.path in options.force_local
    return refetch_guard(
        refetch=lambda: adapter.refetch(ctx, rid),
        expected_hash=_remote_hash(adapter, p.remote) if p.remote is not None else None,
        canonical_hash=lambda fresh: digest(adapter.canonical_remote(fresh.data)),
        forced=forced,
    )


def _write_collision_sidecar(
    root: Path,
    adapter: Adapter,
    p: Plan,
    remote: RemoteRecord | None,
) -> None:
    """The document binding of :func:`grison.engine.sidecar.write_sidecar` (same
    shared write :mod:`grison.engine.filesets` uses for its own, bytes-shaped
    records): renders ``remote`` through the adapter's own ``render_local`` and
    writes the result at ``sidecar_path(p.path)``."""
    path = p.path
    if path is None or remote is None:
        return
    write_sidecar(
        root, path, remote, lambda: adapter.render_local(remote.data, path=path).encode("utf-8")
    )
