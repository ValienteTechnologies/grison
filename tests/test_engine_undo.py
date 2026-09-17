"""Proofs for coordinator feedback items 1 (snapshot contents/summary) and 5 (no
stub restore — the impossible call is unrepresentable via the split protocol)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from grison.engine.adapter import CreateUndoAdapter, RestorableUndoAdapter
from grison.engine.model import RemoteRecord
from grison.engine.undo import Snapshot, UndoOp, describe_snapshot


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
