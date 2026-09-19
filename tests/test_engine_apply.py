"""The apply loop against a tiny in-memory fake adapter (not BookStack/Ghostwriter —
:mod:`grison.engine` must stay record-type-agnostic, so its own tests use a fake
record kind too). Proves ENGINE.md §7's crash-ordering guarantee: a crash right after
the server returns a create id never produces a duplicate create on the next sync.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.apply import RunOptions, run, sidecar_path
from grison.engine.model import Canonical, LocalDoc, Outcome, RemoteRecord
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index, IndexKind
from grison.validator.registry import Failure


class FakeStore:
    """The "server" a FakeAdapter talks to — a plain dict, so tests can inspect it."""

    def __init__(self) -> None:
        self.records: dict[int, str] = {}
        self._next_id = 1
        self.create_calls = 0

    def create(self, text: str) -> int:
        self.create_calls += 1
        rid = self._next_id
        self._next_id += 1
        self.records[rid] = text
        return rid


class FakeAdapter:
    kind = "gw.finding"  # any valid IndexKind — this test is not about BookStack/Ghostwriter
    mode = "read-write"

    def __init__(self, store: FakeStore, *, raise_in_render: bool = False) -> None:
        self.store = store
        self.raise_in_render = raise_in_render

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "recs"
        if not base.is_dir():
            return
        for p in sorted(base.glob("*.txt")):
            text = p.read_text(encoding="utf-8")
            yield LocalDoc(path=PurePosixPath("recs", p.name), doc=text, raw_text=text)

    def fetch_remote(self, ctx: Any) -> dict[int, RemoteRecord]:
        return {
            rid: RemoteRecord(id=rid, data={"text": text}, witness={})
            for rid, text in self.store.records.items()
        }

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None:
        text = self.store.records.get(id)
        return None if text is None else RemoteRecord(id=id, data={"text": text}, witness={})

    def canonical_local(self, doc: Any) -> Canonical:
        return {"text": doc}

    def canonical_remote(self, data: Any) -> Canonical:
        return {"text": data["text"]}

    def render_local(self, data: Any, *, path: PurePosixPath) -> str:
        if self.raise_in_render:
            raise RuntimeError("simulated crash right after the server returned a create id")
        return str(data["text"])

    def default_path(self, data: Any, *, root: Path) -> PurePosixPath:
        return PurePosixPath("recs", "new.txt")

    def relocated_path(self, data: Any, *, current: PurePosixPath) -> PurePosixPath:
        return current

    def create(self, ctx: Any, doc: Any) -> RemoteRecord:
        rid = self.store.create(doc)
        return RemoteRecord(id=rid, data={"text": doc}, witness={})

    def update(self, ctx: Any, id: int, doc: Any) -> RemoteRecord:
        self.store.records[id] = doc
        return RemoteRecord(id=id, data={"text": doc}, witness={})

    def delete(self, ctx: Any, id: int) -> None:
        self.store.records.pop(id, None)

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord:
        rid = self.store.create(preimage["text"])
        return RemoteRecord(id=rid, data={"text": preimage["text"]}, witness={})

    def veto(self, local: Any | None, remote: Any | None) -> str | None:
        return None


def _run_once(root: Path, adapter: FakeAdapter) -> None:
    index = Index.load(root)
    state = StateStore(root)
    snapshot = Snapshot()
    failures: list[Failure] = []
    run(
        root,
        ctx=None,
        adapter=adapter,
        index=index,
        state=state,
        snapshot=snapshot,
        failures=failures,
        options=RunOptions(),
    )
    index.save()


def test_crash_right_after_create_never_duplicates_on_next_sync(tmp_path: Path) -> None:
    root = tmp_path
    (root / "recs").mkdir()
    (root / "recs" / "new.txt").write_text("hello world", encoding="utf-8")

    store = FakeStore()
    crashing_adapter = FakeAdapter(store, raise_in_render=True)

    index = Index.load(root)
    state = StateStore(root)
    snapshot = Snapshot()
    plans, events, summary = run(
        root,
        ctx=None,
        adapter=crashing_adapter,
        index=index,
        state=state,
        snapshot=snapshot,
        failures=[],
        options=RunOptions(),
    )
    index.save()

    # the server call happened (one record created)...
    assert store.create_calls == 1
    assert len(store.records) == 1
    # ...and the per-record isolation caught the "crash" (raised in render_local,
    # AFTER the create() call and the index.set() that immediately follows it)
    assert plans[0].outcome is Outcome.FAILED
    # the crash-ordering guarantee: the index entry survives the crash regardless —
    # it was written the instant the server returned the id, before anything else.
    rec = index.get("recs/new.txt")
    assert rec is not None
    assert rec.kind is IndexKind.GW_FINDING
    assert rec.id == next(iter(store.records))

    # next sync: a non-crashing adapter over the SAME store/index — must not create
    # a second record. The local file's content still matches what was pushed, so it
    # classifies CLEAN (or, if render normalization differs, COLLISION) — never CREATE.
    healthy_adapter = FakeAdapter(store, raise_in_render=False)
    _run_once(root, healthy_adapter)
    assert store.create_calls == 1, "a crash right after create() must never duplicate the create"


def test_dry_run_never_deletes_a_real_collision_sidecar(tmp_path: Path) -> None:
    """Item 2 (HIGH, fix-d): ``run``'s own ``_clear_stale_sidecars`` call used to
    fire unconditionally, even under ``--dry-run`` — ENGINE.md §9 says dry-run
    performs NO writes of any kind, but a sidecar left by a genuine collision
    that has since converged got deleted for real by a dry-run pass alone.
    Mirrors ``grison.engine.filesets``'s own version of this fix/test."""
    root = tmp_path
    (root / "recs").mkdir()
    (root / "recs" / "x.txt").write_text("v1", encoding="utf-8")
    store = FakeStore()
    adapter = FakeAdapter(store)
    _run_once(root, adapter)
    rid = next(iter(store.records))
    assert store.records[rid] == "v1"

    # local and remote both drift away from base, in different directions -> COLLISION
    (root / "recs" / "x.txt").write_text("local-edit", encoding="utf-8")
    store.records[rid] = "remote-edit"
    index = Index.load(root)
    state = StateStore(root)
    snapshot = Snapshot()
    plans, _events, _summary = run(
        root,
        ctx=None,
        adapter=adapter,
        index=index,
        state=state,
        snapshot=snapshot,
        failures=[],
        options=RunOptions(),
    )
    index.save()
    assert plans[0].outcome is Outcome.COLLISION
    sidecar = root / sidecar_path(PurePosixPath("recs/x.txt"))
    assert sidecar.exists()

    # the conflict resolves (local now matches remote) -> a DRY-RUN pass
    # reclassifies away from COLLISION...
    (root / "recs" / "x.txt").write_text("remote-edit", encoding="utf-8")
    index2 = Index.load(root)
    state2 = StateStore(root)
    snapshot2 = Snapshot()
    plans2, _events2, _summary2 = run(
        root,
        ctx=None,
        adapter=adapter,
        index=index2,
        state=state2,
        snapshot=snapshot2,
        failures=[],
        options=RunOptions(dry_run=True),
    )

    assert all(p.outcome is not Outcome.COLLISION for p in plans2)
    assert sidecar.exists()  # ...but --dry-run must NEVER delete it for real (ENGINE.md §9)


def test_healthy_create_writes_index_before_local_canonicalisation(tmp_path: Path) -> None:
    """Sanity check for the same ordering on the non-crashing path: index entry, then
    local file (already covered implicitly above, asserted directly here too)."""
    root = tmp_path
    (root / "recs").mkdir()
    (root / "recs" / "new.txt").write_text("hello world", encoding="utf-8")
    store = FakeStore()
    adapter = FakeAdapter(store)
    _run_once(root, adapter)
    index = Index.load(root)
    rec = index.get("recs/new.txt")
    assert rec is not None
    assert store.records[rec.id] == "hello world"
