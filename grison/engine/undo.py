"""The ONE undo engine, under ``.grison/snapshots/<utc-timestamp>/`` (ENGINE.md
'Undo capture' + D7): 0700 dirs / 0600 files, newest 10 kept, older pruned
automatically after every run. ``grison undo [--list] [SNAPSHOT]`` replays a
snapshot's inverse operations through the adapters, newest-op-first, each one guarded
by the same re-fetch check the forward apply loop uses — a record changed since the
snapshot was taken is reported, not silently overwritten.

Undo is for REMOTE writes. A snapshot only ever holds ops for PUSH/MOVE_EDIT/CREATE/
DELETE_REMOTE (the outcomes in ``grison.engine.apply._REMOTE_WRITE_OUTCOMES``) — never
PULL/PULL_NEW/DELETE_LOCAL/a pure MOVE, which touch only the local, git-tracked tree
and have nothing for grison's own undo to add over `git checkout`/`git mv`. This
matters operationally, not just conceptually: with prune-to-10, a workspace that
recorded every read-only sync too would have its real (remote-write) undo points
evicted by ordinary pull-only syncs. :func:`grison.engine.apply.run` only calls
:meth:`Snapshot.persist` when :attr:`Snapshot.empty` is False, so a run with no remote
writes leaves no snapshot at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import CreateUndoAdapter, RestorableUndoAdapter
from grison.engine.events import verb_for_outcome
from grison.engine.state import StateStore
from grison.fsio import atomic_write_text, ensure_private_dir
from grison.index import Index, IndexKind

SNAPSHOTS_DIR = ".grison/snapshots"
_KEEP = 10


@dataclass
class UndoOp:
    """One reversible REMOTE write, in the order it was applied (replay walks this
    list newest-first, i.e. reversed)."""

    kind: str
    outcome: str  # the Outcome.value this op undoes: create/push/move_edit/delete_remote
    path: str | None  # workspace-relative posix path, or None (an id-only op)
    id: int | None = None
    move_from: str | None = None  # old path, for move_edit
    remote_preimage: Any = None  # adapter's raw record shape before this op's write, or None
    local_preimage: str | None = None
    """For a ``create`` op only: the author's own exact local bytes BEFORE the
    create's post-write mirror rewrite (``LocalDoc.raw_text`` at the moment
    ``adapter.create`` was called). Undoing a create restores these bytes at the
    same path (rather than deleting the file, which would lose the author's
    original words, or leaving it in its post-create rendered form, which was
    never what the author wrote) — see ``_replay_one``'s "create" branch."""


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
        out = root / SNAPSHOTS_DIR / at.strftime("%Y%m%dT%H%M%S.%fZ")
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


@dataclass(frozen=True)
class SnapshotSummary:
    """What ``grison undo --list`` shows for one snapshot: when, and how many of
    each verb it holds — e.g. ``2026-09-17 17:32  push 1``."""

    name: str
    at: datetime
    counts: dict[str, int]

    def render(self) -> str:
        counts = ", ".join(f"{verb} {n}" for verb, n in sorted(self.counts.items()))
        return f"{self.at:%Y-%m-%d %H:%M}  {counts}"


def snapshot_kinds(root: Path, name: str) -> set[str]:
    """Every distinct ``kind`` recorded in snapshot ``name`` — a phase's own
    ``Snapshot`` only ever collects its own kinds (each phase persists its own
    snapshot directory), so this tells a caller with more than one remote domain
    (e.g. the CLI's ``grison undo``, which drives BookStack and Ghostwriter through
    different context objects) which domain's adapters/context a given snapshot
    needs, without guessing from the snapshot's timestamp or name."""
    return {op.kind for op in _load(root, name)}


def describe_snapshot(root: Path, name: str) -> SnapshotSummary:
    at = datetime.strptime(name, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    counts: dict[str, int] = {}
    for op in _load(root, name):
        verb = verb_for_outcome(op.outcome)
        counts[verb] = counts.get(verb, 0) + 1
    return SnapshotSummary(name=name, at=at, counts=counts)


def replay(
    root: Path,
    name: str,
    *,
    ctx: Any,
    adapters: dict[str, CreateUndoAdapter],
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


def _replay_one(  # noqa: PLR0912
    root: Path, op: UndoOp, adapter: CreateUndoAdapter | None, ctx: Any, index: Index,
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
            # Restore the author's own pre-create bytes rather than deleting the
            # file (which would lose their original words) or leaving it in its
            # post-create mirrored form (which was never what they wrote) — an
            # older snapshot recorded before this field existed has no preimage to
            # restore, so it falls back to the previous behaviour (delete the file).
            target = root / op.path
            if op.local_preimage is not None:
                atomic_write_text(target, op.local_preimage)
            else:
                target.unlink(missing_ok=True)
        _emit(on_event, f"delete-remote {op.path or op.id} — undoing create")
        return

    # Every other recorded outcome (push/move_edit/delete_remote) needs restore() —
    # the impossible call (asking a create-only adapter, e.g. bs.book/bs.chapter's
    # BookUndoAdapter, to restore a pre-image it never captured) is unrepresentable:
    # such an adapter is never registered under a kind that records these outcomes
    # in the first place, so this is a defensive check, not an expected path.
    if adapter is None or not isinstance(adapter, RestorableUndoAdapter):
        problems.append(f"{op.path}: adapter for kind {op.kind!r} cannot restore — "
                        "cannot undo this op")
        return

    if op.outcome in ("push", "move_edit"):
        if op.id is None or op.remote_preimage is None:
            return
        adapter.restore(ctx, op.remote_preimage)
        _emit(on_event, f"push {op.path} — restored pre-undo content")
        return

    if op.outcome == "delete_remote":
        if op.remote_preimage is None:
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
            # render_local is only on the full Adapter (RestorableUndoAdapter alone
            # doesn't have it) — a structure kind like bs.book/bs.chapter never
            # records a delete_remote op in the first place, so this is never
            # reached for those; the getattr is defense in depth, not a stub.
            render_local = getattr(adapter, "render_local", None)
            target = root / op.path
            if callable(render_local) and not target.exists():
                atomic_write_text(target, render_local(restored.data, path=PurePosixPath(op.path)))
        _emit(on_event, f"create {op.path} — restored after delete")
        return
