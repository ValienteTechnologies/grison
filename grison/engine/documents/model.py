from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from grison.engine.adapter import Adapter
from grison.engine.common import MASS_CHANGE_RATIO
from grison.engine.model import RemoteRecord
from grison.hashing import digest


@dataclass
class RunOptions:
    dry_run: bool = False
    force_local: frozenset[PurePosixPath] = frozenset()
    force_remote: frozenset[PurePosixPath] = frozenset()
    mass_change_ratio: float = MASS_CHANGE_RATIO
    #: ``grison sync --allow-mass-change``: this run's writes are deliberate in bulk
    #: (a first import, a whole report's worth of new findings) — the guard steps
    #: aside for the run; every write is still undo-snapshotted as usual.
    allow_mass_change: bool = False


def _remote_hash(adapter: Adapter, remote: RemoteRecord | None) -> str | None:
    if remote is None:
        return None
    if remote.cached_hash is not None:
        return remote.cached_hash
    return digest(adapter.canonical_remote(remote.data))
