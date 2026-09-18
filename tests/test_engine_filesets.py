"""The file-set sync loop (:mod:`grison.engine.filesets`) against a tiny in-memory
fake file-set server — not BookStack/Ghostwriter (the mechanism is shared between
D1/D9, and this test proves the mechanism itself, independent of either adapter).

Each test proves one BRIEF task A rule that would NOT hold without
``grison.engine.filesets``:
  - a new local file uploads, a new remote row pulls (rule (a), both directions).
  - a locally-deleted (indexed) file's remote row is deleted.
  - bytes changing under an unchanged name creates a NEW remote row, never an
    in-place update (D1's "no replace bytes" rule).
  - a local caption opinion (non-empty alt) pushes; no opinion mirrors the
    remote's own caption instead of ever inventing a diff on that axis.
  - a mass deletion trips the ONE change guard (D6), leaving the remote rows alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.filesets import RunOptions, sync_fileset
from grison.engine.model import Outcome, RemoteRecord
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.fsio import atomic_write_bytes
from grison.index import Index

FOLDER = PurePosixPath("findings/reports/r1/evidence")


class FakeFileStore:
    """The "server" a FakeFileSetAdapter talks to."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.bodies: dict[int, bytes] = {}
        self._next_id = 1
        self.upload_calls = 0
        self.update_caption_calls = 0
        self.delete_calls = 0
        self.fetch_body_calls = 0

    def upload(self, filename: str, body: bytes, caption: str, description: str) -> int:
        self.upload_calls += 1
        rid = self._next_id
        self._next_id += 1
        self.rows[rid] = {"filename": filename, "caption": caption, "description": description}
        self.bodies[rid] = body
        return rid


@dataclass
class FakeFileSetAdapter:
    store: FakeFileStore
    kind: str = "gw.evidence"
    supports_caption: bool = True

    def list_remote(self, ctx: Any) -> dict[int, RemoteRecord]:
        del ctx
        return {i: RemoteRecord(id=i, data=dict(row)) for i, row in self.store.rows.items()}

    def fetch_body(self, ctx: Any, id: int) -> bytes:
        del ctx
        self.store.fetch_body_calls += 1
        return self.store.bodies[id]

    def upload(
        self, ctx: Any, *, filename: str, body: bytes, caption: str, description: str
    ) -> RemoteRecord:
        del ctx
        rid = self.store.upload(filename, body, caption, description)
        return RemoteRecord(id=rid, data=dict(self.store.rows[rid]))

    def update_caption(
        self, ctx: Any, id: int, *, caption: str, description: str
    ) -> RemoteRecord:
        del ctx
        self.store.update_caption_calls += 1
        self.store.rows[id]["caption"] = caption
        self.store.rows[id]["description"] = description
        return RemoteRecord(id=id, data=dict(self.store.rows[id]))

    def delete(self, ctx: Any, id: int) -> None:
        del ctx
        self.store.delete_calls += 1
        self.store.rows.pop(id, None)
        self.store.bodies.pop(id, None)

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None:
        del ctx
        row = self.store.rows.get(id)
        return RemoteRecord(id=id, data=dict(row)) if row is not None else None

    def restore(self, ctx: Any, preimage: dict[str, Any]) -> RemoteRecord:
        import base64

        rid = self.store.upload(
            preimage["filename"], base64.b64decode(preimage["body_b64"]),
            preimage.get("caption", ""), preimage.get("description", ""),
        )
        return RemoteRecord(id=rid, data=dict(self.store.rows[rid]))

    def remote_label(self, data: Any) -> str:
        return f'"{data.get("filename", "(file)")}"'


def _env(root: Path) -> tuple[Index, StateStore, Snapshot]:
    return Index.load(root), StateStore(root), Snapshot()


def test_new_local_file_uploads(tmp_path: Path) -> None:
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"bytes-1")
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)

    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                          snapshot=snapshot)

    assert store.upload_calls == 1
    assert [p.outcome for p in result.plans] == [Outcome.CREATE]
    assert index.get(str(FOLDER / "shot.png")) is not None


def test_new_remote_row_pulls(tmp_path: Path) -> None:
    store = FakeFileStore()
    rid = store.upload("shot.png", b"remote-bytes", "", "")
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)

    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                          snapshot=snapshot)

    assert [p.outcome for p in result.plans] == [Outcome.PULL_NEW]
    pulled = tmp_path / FOLDER / "shot.png"
    assert pulled.read_bytes() == b"remote-bytes"
    assert index.get(str(FOLDER / "shot.png")).id == rid


def test_locally_deleted_indexed_file_deletes_remote_row(tmp_path: Path) -> None:
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()

    (tmp_path / FOLDER / "shot.png").unlink()
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2)

    assert [p.outcome for p in result.plans] == [Outcome.DELETE_REMOTE]
    assert store.delete_calls == 1
    assert store.rows == {}


def test_bytes_changed_under_same_name_creates_new_row_not_update(tmp_path: Path) -> None:
    """D1: Ghostwriter/BookStack have no "replace bytes" operation — a same-named
    file whose bytes changed must become a new remote row, old one deleted."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    old_id = next(iter(store.rows))

    atomic_write_bytes(tmp_path / FOLDER / "shot.png", b"v2-different-bytes")
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2)

    assert store.upload_calls == 2  # original + reupload
    assert store.delete_calls == 1  # old row removed
    assert old_id not in store.rows
    new_id = next(iter(store.rows))
    assert store.bodies[new_id] == b"v2-different-bytes"
    assert index2.get(str(FOLDER / "shot.png")).id == new_id
    assert [p.outcome for p in result.plans] == [Outcome.MOVE_EDIT]


def test_local_caption_opinion_pushes_and_no_opinion_mirrors_remote(tmp_path: Path) -> None:
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")

    # first sync: no referencing document yet -> no local opinion -> caption stays ""
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    rid = next(iter(store.rows))
    assert store.rows[rid]["caption"] == ""
    assert store.update_caption_calls == 0

    # a referencing document now gives the file a non-empty alt -> PUSH the caption
    doc_path = PurePosixPath("findings/reports/r1/f.md")
    doc_body = "# F\n\n![Login screen](evidence/shot.png)\n"
    index2, state2, snapshot2 = _env(tmp_path)
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2, snapshot=snapshot2,
                doc_bodies={doc_path: doc_body})

    assert store.update_caption_calls == 1
    assert store.rows[rid]["caption"] == "Login screen"


def test_mass_delete_trips_the_change_guard(tmp_path: Path) -> None:
    # Establish the mirror via 8 remote rows pulled down (PULL_NEW is deliberately
    # exempt from the W-side guard -- ENGINE.md/apply.py's own rule, mirrored here --
    # so the *deletion* pass below is the one and only thing this test measures).
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    for i in range(8):
        store.upload(f"shot-{i}.png", f"v{i}".encode(), "", "")
    index, state, snapshot = _env(tmp_path)
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    assert len(store.rows) == 8

    for f in (tmp_path / FOLDER).glob("*"):
        f.unlink()
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2, options=RunOptions(mass_change_ratio=0.2))

    assert all(p.outcome == Outcome.WITHHELD for p in result.plans)
    assert len(store.rows) == 8  # nothing actually deleted while withheld


def test_rename_with_unchanged_bytes_pairs_as_a_move_not_create_plus_delete(
    tmp_path: Path,
) -> None:
    """D3: identical bytes at a new path is a MOVE — index repointed at the same
    remote id, zero remote mutations (no reupload, no delete). Before the fix
    this always fell through to CREATE+DELETE_REMOTE because the move-pairing
    "identical" check compared incompatible hash spaces (a canonical
    body+caption+description digest against a raw bytes hash), so it could
    never fire even for byte-identical content — see
    ``grison.engine.identity.pair``/``filesets._pairing_plans``."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "a.png").write_bytes(b"same-bytes")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    rid = next(iter(store.rows))
    assert store.upload_calls == 1

    (tmp_path / FOLDER / "a.png").rename(tmp_path / FOLDER / "b.png")
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2)

    assert [p.outcome for p in result.plans] == [Outcome.MOVE]
    assert store.upload_calls == 1  # no reupload
    assert store.delete_calls == 0  # no delete
    assert index2.get(str(FOLDER / "a.png")) is None
    assert index2.get(str(FOLDER / "b.png")).id == rid
    assert store.rows[rid]["filename"] == "a.png"  # server side is untouched by a pure rename


def test_clean_sync_after_first_pull_downloads_the_body_exactly_once(tmp_path: Path) -> None:
    """D1's "bytes are immutable for a given id" contract means a clean sync never
    needs to re-download a file it already has a cached digest for — this is the
    fix for classify() unconditionally calling ``fetch_body`` on every record
    every sync (a real cost against actual evidence/image bytes, not just the
    fake)."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")

    result1 = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                           snapshot=snapshot)
    assert [p.outcome for p in result1.plans] == [Outcome.CREATE]
    index.save()

    index2, state2, snapshot2 = _env(tmp_path)
    before = store.fetch_body_calls
    result2 = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                           snapshot=snapshot2)

    assert [p.outcome for p in result2.plans] == [Outcome.CLEAN]
    assert store.fetch_body_calls == before, "a clean sync must not download the body again"


def test_pull_then_clean_sync_of_a_purely_remote_file_downloads_it_only_once(
    tmp_path: Path,
) -> None:
    """Same guarantee as above, from the PULL_NEW direction (a file that started
    out remote-only)."""
    store = FakeFileStore()
    rid = store.upload("shot.png", b"remote-bytes", "", "")
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)

    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    after_pull = store.fetch_body_calls
    assert after_pull >= 1  # the pull itself must download once to materialise the file

    index2, state2, snapshot2 = _env(tmp_path)
    result2 = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                           snapshot=snapshot2)

    assert [p.outcome for p in result2.plans] == [Outcome.CLEAN]
    assert store.fetch_body_calls == after_pull
    assert index2.get(str(FOLDER / "shot.png")).id == rid
