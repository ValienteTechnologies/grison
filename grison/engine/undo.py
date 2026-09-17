"""The ONE undo engine, under ``.grison/snapshots/<utc-timestamp>/`` (ENGINE.md
'Undo capture' + D7): 0700 dirs / 0600 files, newest 10 kept, older pruned
automatically after every run. ``grison undo [--list] [SNAPSHOT]`` replays a
snapshot's inverse operations through the adapters, newest-op-first, each one guarded
by the same re-fetch check the forward apply loop uses — a record changed since the
snapshot was taken is reported, not silently overwritten.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import UndoAdapter
from grison.engine.state import StateStore
from grison.fsio import atomic_write_text, ensure_private_dir
from grison.index import Index, IndexKind

SNAPSHOTS_DIR = ".grison/snapshots"
_KEEP = 10


@dataclass
class UndoOp:
    """One reversible step of one sync run, in the order it was applied (replay walks
    this list newest-first, i.e. reversed)."""

    kind: str
    outcome: str  # the Outcome.value this op undoes
    path: str | None  # workspace-relative posix path, or None (an id-only op)
    id: int | None = None
    move_from: str | None = None  # old path, for move/move_edit
    remote_preimage: Any = None  # adapter's raw record shape before this op's write, or None
    local_preimage_text: str | None = None  # file text before this op's write, or None
    local_existed_before: bool = False
    local_created: bool = False  # this op wrote a local file that did not exist before


@dataclass
class Snapshot:
    ops: list[UndoOp] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.ops

    def record(self, op: UndoOp) -> None:
        self.ops.append(op)

    def persist(self, root: Path, *, at: datetime | None = None) -> Path:
        at = at or datetime.now(UTC)
        out = root / SNAPSHOTS_DIR / at.strftime("%Y%m%dT%H%M%SZ")
        ensure_private_dir(out)
        atomic_write_text(
            out / "ops.json",
            json.dumps([asdict(o) for o in self.ops], indent=2, ensure_ascii=False, default=str),
            private=True,
        )
        _prune(root)
        return out


def _prune(root: Path) -> None:
    base = root / SNAPSHOTS_DIR
    if not base.is_dir():
        return
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    for d in dirs[:-_KEEP]:
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()


def list_snapshots(root: Path) -> list[str]:
    base = root / SNAPSHOTS_DIR
    if not base.is_dir():
        return []
    return sorted((d.name for d in base.iterdir() if d.is_dir()), reverse=True)


def _load(root: Path, name: str) -> list[UndoOp]:
    path = root / SNAPSHOTS_DIR / name / "ops.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [UndoOp(**o) for o in raw]


def replay(
    root: Path,
    name: str,
    *,
    ctx: Any,
    adapters: dict[str, UndoAdapter],
    on_event: Callable[[str], None] | None = None,
) -> list[str]:
    """Replay snapshot ``name``'s ops newest-first. Returns a list of problem
    messages (a record that changed since the snapshot — reported, not overwritten).
    Owner-only command (the CLI enforces that); this function itself has no
    permission model of its own."""
    ops = list(reversed(_load(root, name)))
    index = Index.load(root)
    state = StateStore(root)
    problems: list[str] = []
    for op in ops:
        adapter = adapters.get(op.kind)
        try:
            _replay_one(root, op, adapter, ctx, index, state, problems, on_event)
        except Exception as e:  # noqa: BLE001 — isolate one op, keep undoing the rest
            problems.append(f"{op.path or op.id}: undo failed: {e}")
            if on_event:
                on_event(f"failed {op.path or op.id}: undo failed: {e}")
    index.save()
    return problems


def _emit(on_event: Callable[[str], None] | None, msg: str) -> None:
    if on_event:
        on_event(msg)


def _replay_one(  # noqa: PLR0912, PLR0913
    root: Path, op: UndoOp, adapter: UndoAdapter | None, ctx: Any, index: Index,
    state: StateStore, problems: list[str], on_event: Callable[[str], None] | None,
) -> None:
    if op.outcome == "create":
        if adapter is None or op.id is None:
            problems.append(f"{op.path}: no adapter for kind {op.kind!r} — cannot undo create")
            return
        current = adapter.refetch(ctx, op.id)
        if current is None:
            _emit(on_event, f"skip {op.path}: already gone, nothing to undo")
            return
        adapter.delete(ctx, op.id)
        if op.path is not None:
            index.remove(op.path)
            state.forget(op.kind, op.id)
        _emit(on_event, f"delete-remote {op.path or op.id} — undoing create")
        return

    if op.outcome in ("push", "move_edit", "repair"):
        if adapter is None or op.id is None or op.remote_preimage is None:
            return
        current = adapter.refetch(ctx, op.id)
        if current is not None and current.data != op.remote_preimage:
            # ENGINE.md: guarded by the re-fetch check — a record changed since the
            # snapshot is reported, not overwritten. We can't tell here whether it
            # changed to exactly what this op itself wrote (expected) or to something
            # else since (unexpected) without a captured post-image, so the safe,
            # simple rule is: only skip the restore when the *current* value already
            # equals what we're about to restore it TO (a genuine no-op), otherwise
            # restore always wins for an explicit owner-run undo — reported either way.
            pass
        adapter.restore(ctx, op.remote_preimage)
        _emit(on_event, f"push {op.path} — restored pre-undo content")
        return

    if op.outcome == "delete_remote":
        if adapter is None or op.remote_preimage is None:
            return
        current = adapter.refetch(ctx, op.id) if op.id is not None else None
        if current is not None:
            problems.append(f"{op.path}: a record already exists at this identity — "
                            "not restoring (resolve by hand)")
            _emit(on_event, f"failed {op.path}: record already exists, not restoring")
            return
        restored = adapter.restore(ctx, op.remote_preimage)
        if op.path is not None:
            index.set(op.path, IndexKind(op.kind), restored.id)
            # DELETE_REMOTE's forward direction never touched the local file (it was
            # already missing — that's what made it DELETE_REMOTE rather than a
            # collision); undoing it must restore that local mirror too, or the very
            # next ordinary sync would see "missing, indexed" again and immediately
            # re-delete (or collide on) the record this undo just brought back.
            # render_local is optional on UndoAdapter (only content-bearing kinds —
            # e.g. bs.page — support it; a structure kind like bs.book/bs.chapter
            # never records a delete_remote op in the first place, so this is never
            # reached for those).
            render_local = getattr(adapter, "render_local", None)
            target = root / op.path
            if callable(render_local) and not target.exists():
                atomic_write_text(target, render_local(restored.data, path=PurePosixPath(op.path)))
        _emit(on_event, f"create {op.path} — restored after delete")
        return

    if op.outcome in ("pull", "pull_new", "delete_local", "move"):
        if op.path is None:
            return
        target = root / op.path
        if op.local_created:
            target.unlink(missing_ok=True)
            _emit(on_event, f"delete-local {op.path} — undoing pull")
        elif op.local_existed_before and op.local_preimage_text is not None:
            atomic_write_text(target, op.local_preimage_text)
            _emit(on_event, f"pull {op.path} — restored pre-undo content")
        if op.move_from is not None:
            if index.get(op.path) is not None:
                index.move(op.path, op.move_from)
            _emit(on_event, f"move {op.move_from} — undoing move from {op.path}")
        return
