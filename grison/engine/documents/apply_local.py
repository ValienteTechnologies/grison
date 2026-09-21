from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.engine.adapter import Adapter
from grison.engine.common import dedupe_path
from grison.engine.events import emit_losses
from grison.engine.model import Event, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.fsio import atomic_write_text
from grison.hashing import digest
from grison.index import Index, IndexKind


def _dedupe(path: PurePosixPath, existing: set[PurePosixPath], root: Path) -> PurePosixPath:
    return dedupe_path(path, lambda c: c in existing or (root / c).exists())


def _apply_pull(  # noqa: PLR0913
    root: Path,
    adapter: Adapter,
    p: Plan,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    events: list[Event],
    dry: bool,
) -> None:
    assert p.remote is not None
    path: PurePosixPath
    relocated_from: PurePosixPath | None = None
    if p.path is None:
        path = adapter.default_path(p.remote.data, root=root)
        existing = {PurePosixPath(rp) for rp in index.records}
        path = _dedupe(path, existing, root)
    else:
        # PULL-side relocation: the record's remote parent may have changed (a page
        # moved to another chapter on the server) — the file must physically move
        # too, or the next sync would see the (now stale) directory disagree with
        # the fresh content and push it straight back. Never a silent overwrite:
        # this only fires for an already-indexed record's own path.
        path = adapter.relocated_path(p.remote.data, current=p.path)
        if path != p.path:
            relocated_from = p.path
    text = adapter.render_local(p.remote.data, path=path)
    target = root / path
    existed = target.exists()
    if relocated_from is not None and existed:
        # never a silent overwrite: an unrelated file already occupies the
        # relocation target — surfaced as a FAILED outcome (per-record isolation),
        # the old file left exactly where it was, for the owner to resolve by hand.
        raise RuntimeError(f"relocation target exists: {path} — resolve manually")
    detail = f"from {relocated_from}" if relocated_from is not None else ""
    if dry:
        events.append(Event(verb="pull", path=str(path), detail=detail, dry_run=True))
        return
    # No undo entry: PULL/PULL_NEW is not a remote write (ENGINE.md 'Undo capture' —
    # "undo is for remote writes"; grison.engine.undo's own module docstring). The
    # local, git-tracked tree already has its own history for this.
    atomic_write_text(target, text)
    if relocated_from is not None:
        (root / relocated_from).unlink(missing_ok=True)
        index.move(str(relocated_from), str(path))
    else:
        index.set(str(path), IndexKind(adapter.kind), p.remote.id)
    state.put(
        adapter.kind,
        p.remote.id,
        base=digest(adapter.canonical_remote(p.remote.data)),
        witness=p.remote.witness,
    )
    emit_losses(events, str(path), p.remote.losses)
    events.append(Event(verb="pull", path=str(path), detail=detail))
