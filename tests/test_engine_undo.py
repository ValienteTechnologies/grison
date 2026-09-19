"""Proofs for coordinator feedback items 1 (snapshot contents/summary) and 5 (no
stub restore — the impossible call is unrepresentable via the split protocol)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from grison.engine.adapter import CreateUndoAdapter, PushUndoAdapter, RestorableUndoAdapter
from grison.engine.model import RemoteRecord
from grison.engine.undo import Snapshot, UndoOp, describe_snapshot, replay
from grison.hashing import digest


class _CreateOnlyAdapter:
    """Shape of grison.adapters.bs_structure.BookUndoAdapter/ChapterUndoAdapter —
    deliberately has NO restore method at all."""

    kind = "bs.book"

    def refetch(self, ctx: object, id: int) -> RemoteRecord | None:
        return None

    def delete(self, ctx: object, id: int) -> None:
        pass


class _FullAdapter(_CreateOnlyAdapter):
    kind = "bs.page"

    def restore(self, ctx: object, preimage: object) -> RemoteRecord:
        return RemoteRecord(id=1, data=preimage)


def test_create_only_adapter_satisfies_create_undo_adapter() -> None:
    assert isinstance(_CreateOnlyAdapter(), CreateUndoAdapter)


def test_create_only_adapter_does_not_satisfy_restorable_undo_adapter() -> None:
    """The impossible call (asking a create-only adapter to restore a pre-image it
    never captured) is unrepresentable — no NotImplementedError stub needed."""
    assert not isinstance(_CreateOnlyAdapter(), RestorableUndoAdapter)
    assert not hasattr(_CreateOnlyAdapter(), "restore")


def test_full_adapter_satisfies_both() -> None:
    assert isinstance(_FullAdapter(), CreateUndoAdapter)
    assert isinstance(_FullAdapter(), RestorableUndoAdapter)


def test_restorable_without_canonical_remote_does_not_satisfy_push_undo_adapter() -> None:
    """Item 4 (fix-d): a push/move_edit undo needs ``canonical_remote`` (drift
    detection) on top of ``restore`` — ``_FullAdapter`` (shape of
    ``bs_structure``'s create-only siblings' RESTORABLE cousin, e.g. a plain
    ``bs.page``-shaped adapter with no ``canonical_remote``) satisfies
    ``RestorableUndoAdapter`` but not the narrower ``PushUndoAdapter``."""
    assert isinstance(_FullAdapter(), RestorableUndoAdapter)
    assert not isinstance(_FullAdapter(), PushUndoAdapter)


class _PushableAdapter(_FullAdapter):
    """A ``RestorableUndoAdapter`` that also has ``canonical_remote`` — satisfies
    ``PushUndoAdapter``, so ``replay`` can run its push/move_edit drift guard
    against it (item 4)."""

    kind = "bs.page"

    def __init__(self) -> None:
        self.store: dict[int, str] = {1: "current"}

    def refetch(self, ctx: object, id: int) -> RemoteRecord | None:
        text = self.store.get(id)
        return None if text is None else RemoteRecord(id=id, data={"text": text})

    def delete(self, ctx: object, id: int) -> None:
        self.store.pop(id, None)

    def restore(self, ctx: object, preimage: object) -> RemoteRecord:
        assert isinstance(preimage, dict)
        self.store[1] = preimage["text"]
        return RemoteRecord(id=1, data=preimage)

    def canonical_remote(self, data: object) -> object:
        assert isinstance(data, dict)
        return {"text": data["text"]}


def test_push_undo_refuses_to_clobber_a_record_edited_again_since(tmp_path: Path) -> None:
    """Item 4 (HIGH, fix-d): the push/move_edit replay branch used to call
    ``adapter.restore`` straight over ``remote_preimage`` with no re-fetch guard
    at all — unlike create/delete_remote. A record edited again (on the server)
    after the push this op would undo must be reported, never clobbered."""
    adapter = _PushableAdapter()
    snap = Snapshot()
    snap.record(UndoOp(
        kind="bs.page", outcome="push", path="a.md", id=1,
        remote_preimage={"text": "original"},
        post_write_hash=digest({"text": "current"}),  # what replay expects to still see
    ))
    name = snap.persist(tmp_path).name

    # someone edits the record again after the push this op would undo
    adapter.store[1] = "edited again"

    problems = replay(tmp_path, name, ctx=None, adapters={"bs.page": adapter})

    assert len(problems) == 1
    assert "a.md" in problems[0]
    assert adapter.store[1] == "edited again"  # never clobbered


def test_push_undo_restores_when_nothing_drifted(tmp_path: Path) -> None:
    """The happy path still works: nothing changed since the push, so the
    pre-image restores normally."""
    adapter = _PushableAdapter()
    snap = Snapshot()
    snap.record(UndoOp(
        kind="bs.page", outcome="push", path="a.md", id=1,
        remote_preimage={"text": "original"},
        post_write_hash=digest({"text": "current"}),
    ))
    name = snap.persist(tmp_path).name

    problems = replay(tmp_path, name, ctx=None, adapters={"bs.page": adapter})

    assert problems == []
    assert adapter.store[1] == "original"


def test_push_undo_with_no_post_write_hash_is_backward_compatible(tmp_path: Path) -> None:
    """An older snapshot recorded before ``post_write_hash`` existed has ``None``
    for it — ``refetch_guard`` already treats ``expected_hash=None`` as "not
    drifted" (nothing to compare against), so an old snapshot's push undo still
    restores rather than being refused outright."""
    adapter = _PushableAdapter()
    snap = Snapshot()
    snap.record(UndoOp(
        kind="bs.page", outcome="push", path="a.md", id=1,
        remote_preimage={"text": "original"},
    ))
    name = snap.persist(tmp_path).name

    problems = replay(tmp_path, name, ctx=None, adapters={"bs.page": adapter})

    assert problems == []
    assert adapter.store[1] == "original"


def test_snapshot_empty_by_default() -> None:
    assert Snapshot().empty


def test_snapshot_persist_only_when_asked_and_prune_keeps_ten(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(12):
        snap = Snapshot()
        snap.record(UndoOp(kind="bs.page", outcome="push", path=f"p{i}.md", id=i))
        snap.persist(tmp_path, at=start + timedelta(seconds=i))
    base = tmp_path / ".grison" / "snapshots"
    assert len(list(base.iterdir())) == 10  # pruned to the newest 10


def test_describe_snapshot_counts_per_verb(tmp_path: Path) -> None:
    snap = Snapshot()
    snap.record(UndoOp(kind="bs.page", outcome="push", path="a.md", id=1))
    snap.record(UndoOp(kind="bs.page", outcome="push", path="b.md", id=2))
    snap.record(UndoOp(kind="bs.page", outcome="create", path="c.md", id=3))
    out = snap.persist(tmp_path)

    summary = describe_snapshot(tmp_path, out.name)

    assert summary.counts == {"push": 2, "create": 1}
    assert "push 2" in summary.render()
    assert "create 1" in summary.render()
    # human time, not the raw "20260917T171940Z" form
    assert summary.at.year >= 2020
    assert ":" in summary.render().split("  ")[0]
