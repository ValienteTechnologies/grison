"""The ONE undo engine, under ``.grison/snapshots/<utc-timestamp>/`` (ENGINE.md
'Undo capture' + D7): 0700 dirs / 0600 files, newest 10 kept, older pruned
automatically after every run. ``grison undo [--list] [SNAPSHOT]`` replays a
snapshot's inverse operations through the adapters, newest-op-first, each one guarded
by the same re-fetch check the forward apply loop uses — a record changed since the
snapshot was taken is reported, not silently overwritten.

One snapshot per ``grison sync`` RUN, not one per phase: :func:`grison.cli.sync`
opens a single :class:`Snapshot` before its first phase and threads the SAME object
through the report, findings and wiki phases, so every remote write from one run —
whichever phase made it, Ghostwriter or BookStack alike — lands in one ops list in
insertion order and is undone by one ``grison undo``, newest write first, regardless
of which phase wrote it. This matches the user's mental model ("undo the last
sync"): before this, each phase persisted its own snapshot, so a sync that both
uploaded evidence (report phase) and pushed a library finding (findings phase) left
two snapshot directories and a single ``grison undo`` only reversed the newer one.

Undo is for REMOTE writes. A snapshot only ever holds ops for PUSH/MOVE_EDIT/CREATE/
DELETE_REMOTE (the outcomes in ``grison.engine.apply._REMOTE_WRITE_OUTCOMES``) — never
PULL/PULL_NEW/DELETE_LOCAL/a pure MOVE, which touch only the local, git-tracked tree
and have nothing for grison's own undo to add over `git checkout`/`git mv`. This
matters operationally, not just conceptually: with prune-to-10, a workspace that
recorded every read-only sync too would have its real (remote-write) undo points
evicted by ordinary pull-only syncs. :func:`grison.cli.sync` only calls
:meth:`Snapshot.persist` when :attr:`Snapshot.empty` is False (i.e. at least one
phase made a remote write), so a run with no remote writes at all leaves no
snapshot — the keep-10 pruning in :func:`_prune` therefore counts RUNS, one
directory per sync that wrote something, never phases within a run.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import CreateUndoAdapter, PushUndoAdapter, RestorableUndoAdapter
from grison.engine.events import verb_for_outcome
from grison.engine.state import StateStore
from grison.fsio import atomic_write_bytes, atomic_write_text, ensure_private_dir
from grison.hashing import digest
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
    """The exact local text to restore at ``path`` when undoing this op — two
    different meanings depending on ``outcome``, both computed at snapshot
    time, never re-derived at replay time:

      - ``create``: the author's own exact bytes BEFORE the create's post-
        write mirror rewrite (``LocalDoc.raw_text`` at the moment
        ``adapter.create`` was called). Undoing restores these bytes rather
        than deleting the file (which would lose the author's original words)
        or leaving it in its post-create rendered form (never what the author
        wrote).
      - ``push``/``move_edit``: ``adapter.render_local(remote_preimage, ...)``
        — the file as it would read if it mirrored the OLD, pre-push remote
        state (exactly like a genuine pull of it would have written it), NOT
        the author's about-to-be-pushed text (that's the very edit undo is
        supposed to revert). Item 3 (fix-findings): this half used to be
        missing entirely — the remote side reverted correctly, but the local
        file kept its post-push content, including a reference to a file a
        SIBLING op in the same snapshot might undo the existence of,
        producing a spurious collision on the next sync — see
        ``_replay_one``'s "push"/"move_edit" branch. A file-set adapter's
        caption-only push (never touches the evidence file's own bytes) has
        no local content to restore at all, so its own writer
        (``grison.engine.filesets._apply_caption_push``) leaves this ``None``
        on purpose — ``_replay_one`` skips the local write entirely rather
        than guessing at a rendering no ``render_local`` method exists for.

    ``None`` for a ``delete_remote`` op too — there is no local file to
    preimage (it was already missing, that's what made it DELETE_REMOTE); the
    local mirror it restores instead comes fresh from the restored remote
    record, see ``_restore_local_mirror``."""
    post_write_hash: str | None = None
    """For a ``push``/``move_edit`` op only: the canonical hash of what the
    record looked like immediately AFTER this op's write (the same hash the
    forward apply loop stamps as the new ``state`` base — ENGINE.md §6's
    canonicalisation-after-push, reused here) — what ``_replay_one`` compares a
    fresh re-fetch against before restoring ``remote_preimage`` over it, via the
    same :func:`grison.engine.apply.refetch_guard` the forward loop uses for its
    own pre-write guard, so a record edited again since this run's write is
    reported instead of clobbered. ``None`` only for an older snapshot recorded
    before this field existed, or an op this guard doesn't apply to
    (create/delete_remote use existence checks instead — see ``_replay_one``) —
    ``refetch_guard`` already treats ``expected_hash=None`` as "not drifted"."""


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
    """Every distinct ``kind`` recorded in snapshot ``name`` — one run's ``Snapshot``
    now collects every phase's kinds together (Ghostwriter's ``gw.*`` and
    BookStack's ``bs.*`` alike, whichever phases actually wrote), so this tells a
    caller with more than one remote domain (e.g. the CLI's ``grison undo``, which
    drives BookStack and Ghostwriter through different context objects) which
    domain's adapters/context a given snapshot needs, without guessing from the
    snapshot's timestamp or name — and without assuming a snapshot is homogeneous."""
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


def _restore_local_mirror(target: Path, adapter: Any, restored: Any, op: UndoOp) -> None:
    """Write back the local file a DELETE_REMOTE undo brings a record back for.
    Two shapes of local content exist: a file-set record's (``gw.evidence``,
    ``bs.image``) pre-image carries the exact bytes it was deleted with
    (``body_b64`` — see :func:`grison.engine.filesets._preimage`, "deletes keep
    the bytes in the snapshot"), so those are written back verbatim; every other
    kind is a text document rendered fresh from the restored record via the full
    ``Adapter``'s ``render_local`` (``bs.page``, ``gw.finding``,
    ``gw.reportedFinding``). A structure kind (``bs.book``/``bs.chapter``) never
    records a delete_remote op in the first place (:class:`CreateUndoAdapter` has
    neither ``render_local`` nor a bytes pre-image), so neither branch fires for
    it — this is defense in depth, not a stub."""
    body_b64 = op.remote_preimage.get("body_b64") if isinstance(op.remote_preimage, dict) else None
    if body_b64 is not None:
        atomic_write_bytes(target, base64.b64decode(body_b64))
        return
    if op.path is None:
        return
    render_local = getattr(adapter, "render_local", None)
    if callable(render_local):
        atomic_write_text(target, render_local(restored.data, path=PurePosixPath(op.path)))


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
        if not isinstance(adapter, PushUndoAdapter):
            problems.append(f"{op.path}: adapter for kind {op.kind!r} cannot verify it is "
                            "still safe to restore — not restoring")
            return
        # No backward-compat fallback: every push/move_edit op recorded by the
        # current forward apply loop always carries a post_write_hash (item 4)
        # — there are no old snapshots to read (keep-10 pruning), so a missing
        # one is a corrupt snapshot, refused outright rather than guessed at
        # (never a silent, unguarded restore).
        if op.post_write_hash is None:
            problems.append(f"{op.path}: snapshot has no post-write hash recorded for this "
                            "push — corrupt snapshot, not restoring")
            return
        # The same pre-write re-fetch guard the forward apply loop runs before
        # ANY remote write (ENGINE.md §3), turned around for undo: a record
        # edited again since the write this op undoes must be reported, not
        # silently overwritten with the older `remote_preimage` — this is the
        # gap the module docstring calls out ("unlike the create and
        # delete_remote branches"). Local import: `grison.engine.apply` imports
        # this module (`Snapshot`/`UndoOp`) at its own top level, so importing it
        # back from here at module scope would be circular.
        from grison.engine.apply import refetch_guard
        rid = op.id
        _fresh, drifted = refetch_guard(
            refetch=lambda: adapter.refetch(ctx, rid),
            expected_hash=op.post_write_hash,
            canonical_hash=lambda fresh: digest(adapter.canonical_remote(fresh.data)),
            forced=False,
        )
        if drifted:
            problems.append(f"{op.path}: changed since the sync that wrote it — "
                            "not restoring (resolve by hand)")
            _emit(on_event, f"failed {op.path}: changed since the sync, not restoring")
            return
        restored = adapter.restore(ctx, op.remote_preimage)
        # The LOCAL half of this undo (item 3, fix-findings): the forward push
        # rewrote the file to the server's rendering (ENGINE.md §6) — undoing
        # the remote write without ALSO putting the file back to how it read
        # BEFORE that push would leave it on its post-push content forever (a
        # dangling reference to something a SIBLING op in the same snapshot
        # might undo the existence of — e.g. a re-created evidence file this
        # push referenced — produces exactly the spurious collision the next
        # sync then hits). `local_preimage` is `None` only for a kind with no
        # local file content to restore at all (a file-set caption push —
        # see the field's own docstring), never a document push/move_edit —
        # the "restored" message is only ever printed once this write has
        # actually happened, not merely because the remote side succeeded.
        if op.path is not None and op.local_preimage is not None:
            atomic_write_text(root / op.path, op.local_preimage)
            _emit(on_event, f"push {op.path} — restored pre-undo content")
        else:
            _emit(on_event, f"push {op.path or op.id} — remote reverted")
        # Restamp state's base to the (reverted) record's OWN canonical hash —
        # ENGINE.md §6's canonicalisation-after-push, run in reverse — so the
        # very next ordinary sync classifies this record CLEAN outright, not a
        # one-off REPAIR (harmless, but not what "undo" promises: the record
        # should look exactly as if the undone push never happened). `adapter`
        # already satisfies `PushUndoAdapter` (checked above), which is exactly
        # `RestorableUndoAdapter` plus this same `canonical_remote`.
        state.put(op.kind, op.id, base=digest(adapter.canonical_remote(restored.data)),
                 witness=restored.witness)
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
            target = root / op.path
            if not target.exists():
                _restore_local_mirror(target, adapter, restored, op)
        _emit(on_event, f"create {op.path} — restored after delete")
        return
