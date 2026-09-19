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

from grison.engine.apply import sidecar_path
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


# --- pre-write re-fetch guard (ENGINE.md §3) ---------------------------------


@dataclass
class DriftingAdapter(FakeFileSetAdapter):
    """Wraps :meth:`FakeFileSetAdapter.refetch` to mutate the remote row's caption
    the FIRST time it's called — simulating a concurrent remote edit discovered
    exactly where the pre-write re-fetch guard looks for it (``refetch``), never
    at classification time (which already read the pre-drift row via
    ``list_remote``). ``refetch`` is the ONE call site the guard uses before any
    remote-destructive write (reupload/caption-push/delete-remote — see
    :mod:`grison.engine.filesets`'s own ``_refetch_guard``), so this is enough to
    prove the guard fires for every one of them without a real server."""

    drift_applied: bool = False
    drift_caption: str = "changed concurrently"

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None:
        if not self.drift_applied:
            self.drift_applied = True
            row = self.store.rows.get(id)
            if row is not None:
                row["caption"] = self.drift_caption
        return super().refetch(ctx, id)


def test_caption_push_drift_since_classification_is_a_collision_not_an_overwrite(
    tmp_path: Path,
) -> None:
    """The caption-push apply step used to only check ``fresh is None`` — a
    caption that changed on the server between classification and the write was
    silently overwritten. Now it goes through the SAME pre-write re-fetch guard
    the document engine uses (:func:`grison.engine.apply.refetch_guard`)."""
    store = FakeFileStore()
    adapter = DriftingAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    rid = next(iter(store.rows))
    assert store.rows[rid]["caption"] == ""

    doc_path = PurePosixPath("findings/reports/r1/f.md")
    doc_body = "# F\n\n![Login screen](evidence/shot.png)\n"
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2, doc_bodies={doc_path: doc_body})

    assert [p.outcome for p in result.plans] == [Outcome.COLLISION]
    assert store.update_caption_calls == 0  # refused, never overwrote the concurrent edit
    assert store.rows[rid]["caption"] == "changed concurrently"  # concurrent edit preserved


def test_reupload_drift_since_classification_is_a_collision_not_an_overwrite(
    tmp_path: Path,
) -> None:
    """The re-upload apply step (bytes changed under the same name) used to only
    check ``fresh is None`` before deleting the old row — a concurrent remote
    edit (or the row vanishing) between classification and the write went
    undetected. Now it collides instead of uploading/deleting anything."""
    store = FakeFileStore()
    adapter = DriftingAdapter(store)
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

    assert [p.outcome for p in result.plans] == [Outcome.COLLISION]
    assert store.upload_calls == 1  # only the original upload — no reupload happened
    assert store.delete_calls == 0  # old row never deleted
    assert old_id in store.rows
    assert store.rows[old_id]["caption"] == "changed concurrently"  # concurrent edit preserved
    assert index2.get(str(FOLDER / "shot.png")).id == old_id  # index untouched


def test_delete_remote_drift_since_classification_is_a_collision_not_a_delete(
    tmp_path: Path,
) -> None:
    """The delete-remote apply step used to only check ``fresh is None`` — a
    concurrent remote edit between classification and the write was silently
    deleted along with everything else. Now it collides instead."""
    store = FakeFileStore()
    adapter = DriftingAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    rid = next(iter(store.rows))

    (tmp_path / FOLDER / "shot.png").unlink()
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2)

    assert [p.outcome for p in result.plans] == [Outcome.COLLISION]
    assert store.delete_calls == 0
    assert rid in store.rows
    assert store.rows[rid]["caption"] == "changed concurrently"  # concurrent edit preserved


# --- duplicate remote filenames in one sync (BRIEF fix-b task 1) ------------


def test_two_remote_rows_with_the_same_filename_dedupe_against_each_other(tmp_path: Path) -> None:
    """Before the fix, ``_dedupe_path`` only checked pre-sync disk/index state, not
    sibling PULL_NEW plans built earlier in the SAME loop: two remote rows named
    identically both computed the SAME dedupe target, and ``_apply_pull`` for the
    second one silently clobbered the first — on disk AND in the index — with no
    event and exit 0. Now a running ``claimed_names`` set is extended after every
    dedupe, so the second row lands at ``shot-2.png`` in the same run."""
    store = FakeFileStore()
    store.upload("shot.png", b"first", "", "")
    store.upload("shot.png", b"second", "", "")
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)

    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                          snapshot=snapshot)

    assert [p.outcome for p in result.plans] == [Outcome.PULL_NEW, Outcome.PULL_NEW]
    on_disk = sorted(p.name for p in (tmp_path / FOLDER).iterdir())
    assert on_disk == ["shot-2.png", "shot.png"]
    bytes_by_name = {p.name: p.read_bytes() for p in (tmp_path / FOLDER).iterdir()}
    assert {bytes_by_name["shot.png"], bytes_by_name["shot-2.png"]} == {b"first", b"second"}
    index.save()
    shot = index.get(str(FOLDER / "shot.png"))
    shot2 = index.get(str(FOLDER / "shot-2.png"))
    assert shot is not None and shot2 is not None
    assert shot.id != shot2.id  # two distinct records, not one clobbering the other

    # both files are now correctly tracked — a follow-up sync is clean, not a
    # repeat "new remote row" for whichever one used to get silently overwritten.
    index2, state2, snapshot2 = _env(tmp_path)
    result2 = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                           snapshot=snapshot2)
    assert [p.outcome for p in result2.plans] == [Outcome.CLEAN, Outcome.CLEAN]


# --- caption conflict never aborts the sync (BRIEF fix-b task 2) ------------


def test_caption_conflict_degrades_only_that_file_and_does_not_abort_the_sync(
    tmp_path: Path,
) -> None:
    """Before the fix, a caption disagreement between two referencing documents
    raised ``FileSetError`` straight out of ``collect_captions`` — called before
    any per-record plan exists, outside ``_apply_one``'s per-record isolation, so
    the exception propagated out of ``sync_fileset`` entirely. Now the conflicting
    file degrades to "no local caption opinion" (module docstring) and gets its
    own FAILED record/`failed` event naming REF-004; every other file in the same
    folder still syncs normally."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    (tmp_path / FOLDER / "other.png").write_bytes(b"v2")

    doc_a = PurePosixPath("findings/reports/r1/a.md")
    doc_b = PurePosixPath("findings/reports/r1/b.md")
    doc_bodies = {
        doc_a: "# A\n\n![Login screen](evidence/shot.png)\n",
        doc_b: "# B\n\n![Different caption](evidence/shot.png)\n",
    }

    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                          snapshot=snapshot, doc_bodies=doc_bodies)

    plans_by_path = [(p.path, p.outcome) for p in result.plans]
    assert (FOLDER / "shot.png", Outcome.FAILED) in plans_by_path  # the conflict, isolated...
    assert (FOLDER / "shot.png", Outcome.CREATE) in plans_by_path  # ...never blocks its own sync
    assert (FOLDER / "other.png", Outcome.CREATE) in plans_by_path  # ...or any other file's
    assert store.upload_calls == 2  # both files still uploaded — the sync was never aborted
    assert result.summary.counts.get("failed") == 1  # affects the exit code like any failed record

    failed_events = [e for e in result.events if e.verb == "failed"]
    assert len(failed_events) == 1
    assert "REF-004" in failed_events[0].detail
    assert str(doc_a) in failed_events[0].detail
    assert str(doc_b) in failed_events[0].detail

    # the disputed caption itself was never pushed — degrades to "no opinion"
    shot_row = next(r for r in store.rows.values() if r["filename"] == "shot.png")
    assert shot_row["caption"] == ""
    other_row = next(r for r in store.rows.values() if r["filename"] == "other.png")
    assert other_row["filename"] == "other.png"


# --- collision sidecars for file-set bytes (BRIEF fix-b task 3) -------------


def test_collision_sidecar_file_is_never_treated_as_an_ordinary_local_file(
    tmp_path: Path,
) -> None:
    """The old sidecar filter (``name.endswith(".remote")``) only ever matched an
    extension-less sidecar — a REAL ``<name>.remote.<ext>`` sidecar (the only kind
    an evidence/image folder ever actually has, since every file in one has an
    extension) always ends in the ORIGINAL extension, not literally ``.remote``,
    so it silently passed straight through ``_local_files`` as an ordinary new
    local file and would have been uploaded as bogus new evidence."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    (tmp_path / FOLDER / "shot.remote.png").write_bytes(b"stale-sidecar-bytes")

    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state,
                          snapshot=snapshot)

    assert [p.outcome for p in result.plans] == [Outcome.CREATE]  # shot.png only
    assert store.upload_calls == 1
    assert next(iter(store.rows.values()))["filename"] == "shot.png"


def test_cold_witness_cache_delete_remote_is_not_a_false_collision(tmp_path: Path) -> None:
    """Item 1 (HIGH, fix-d): ``_refetch_guard`` used to default a cache-miss body
    hash to ``""`` while classification computed the REAL one from a live
    download — on a cold cache (state's ``witness`` empty, base still intact)
    every DELETE_REMOTE/reupload/caption push false-COLLIDED with a spurious
    sidecar written next to a file that no longer even exists locally. Fixed on
    both halves: ``_classify_missing`` warms the cache like ``_classify_one``,
    and the guard downloads via ``fetch_body`` on a genuine miss instead of
    defaulting to ``""``."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()
    rid = next(iter(store.rows))
    base = state.get(adapter.kind, rid).base
    assert base is not None

    # Simulate a cold witness cache: the base survives, the cached body_hash
    # doesn't (state lost partially, or a prior classify-only pass — see the
    # module docstring's "state lost" cold-cache cause).
    state.put(adapter.kind, rid, base=base, witness={})

    (tmp_path / FOLDER / "shot.png").unlink()
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2)

    assert [p.outcome for p in result.plans] == [Outcome.DELETE_REMOTE]
    assert store.delete_calls == 1
    assert store.rows == {}
    sidecar = tmp_path / FOLDER / "shot.remote.png"
    assert not sidecar.exists()


def test_dry_run_never_deletes_a_real_collision_sidecar(tmp_path: Path) -> None:
    """Item 2 (HIGH, fix-d): ``sync_fileset``'s own ``_clear_stale_sidecars`` call
    used to run unconditionally, even under ``--dry-run`` — ENGINE.md §9 says
    dry-run performs NO writes of any kind, but a sidecar left by a genuine
    collision that has since converged got deleted for real by a dry-run pass
    alone."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    doc_path = PurePosixPath("findings/reports/r1/f.md")

    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot,
                doc_bodies={doc_path: "# F\n\n![Local caption](evidence/shot.png)\n"})
    index.save()
    rid = next(iter(store.rows))

    # someone edits the caption directly on the server -> a genuine classify-time
    # collision (same setup as the resolved-sidecar test below).
    store.rows[rid]["caption"] = "Remote caption"
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(
        tmp_path, store, adapter, FOLDER, index=index2, state=state2, snapshot=snapshot2,
        doc_bodies={doc_path: "# F\n\n![A different local caption](evidence/shot.png)\n"},
    )
    index2.save()
    assert [p.outcome for p in result.plans] == [Outcome.COLLISION]
    sidecar = tmp_path / FOLDER / "shot.remote.png"
    assert sidecar.exists()

    # the conflict resolves (local now agrees with remote) -> a DRY-RUN pass
    # reclassifies away from COLLISION...
    index3, state3, snapshot3 = _env(tmp_path)
    result2 = sync_fileset(
        tmp_path, store, adapter, FOLDER, index=index3, state=state3, snapshot=snapshot3,
        doc_bodies={doc_path: "# F\n\n![Remote caption](evidence/shot.png)\n"},
        options=RunOptions(dry_run=True),
    )

    assert all(p.outcome is not Outcome.COLLISION for p in result2.plans)
    assert sidecar.exists()  # ...but --dry-run must NEVER delete it for real (ENGINE.md §9)


def test_dry_run_on_a_cold_cache_never_writes_state(tmp_path: Path) -> None:
    """Item 3 (MEDIUM, fix-d): ``_classify_one``/``_classify_missing`` used to
    call ``state.put`` (warming the body-hash cache) even under ``--dry-run``,
    contradicting ENGINE.md §9 ("no writes of any kind"). A dry-run sync must
    leave ``.grison/state/`` completely untouched — cold cache or not, since a
    cold cache is exactly when this warming call fires."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot)
    index.save()

    # Cold cache: state gone entirely (nothing under this test writes it again
    # except the dry run below, which must not).
    state_dir = tmp_path / ".grison" / "state"
    assert state_dir.is_dir()
    for f in state_dir.rglob("*"):
        if f.is_file():
            f.unlink()
    for d in sorted(state_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if d.is_dir():
            d.rmdir()
    state_dir.rmdir()
    assert not state_dir.exists()

    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(tmp_path, store, adapter, FOLDER, index=index2, state=state2,
                          snapshot=snapshot2, options=RunOptions(dry_run=True))

    assert [p.outcome for p in result.plans] == [Outcome.CLEAN]  # L == R even with no base
    assert not state_dir.exists(), "a dry-run classify must never warm/write private state"


def test_collision_writes_a_remote_bytes_sidecar_cleared_once_resolved(tmp_path: Path) -> None:
    """ENGINE.md §8: a file-set COLLISION must write ``<name>.remote.<ext>`` next
    to the file with the REMOTE version's bytes — before this fix, the classify-
    time COLLISION branch in ``_dispatch`` only appended an event, leaving
    nothing on disk. The sidecar is cleared as soon as the record is no longer in
    collision (here: the two sides converge)."""
    store = FakeFileStore()
    adapter = FakeFileSetAdapter(store)
    index, state, snapshot = _env(tmp_path)
    (tmp_path / FOLDER).mkdir(parents=True)
    (tmp_path / FOLDER / "shot.png").write_bytes(b"v1")
    doc_path = PurePosixPath("findings/reports/r1/f.md")

    sync_fileset(tmp_path, store, adapter, FOLDER, index=index, state=state, snapshot=snapshot,
                doc_bodies={doc_path: "# F\n\n![Local caption](evidence/shot.png)\n"})
    index.save()
    rid = next(iter(store.rows))
    assert store.rows[rid]["caption"] == "Local caption"

    # someone edits the caption directly on the server (no grison involved) —
    # discovered at the very next sync's bulk list, no pre-write re-fetch needed:
    # a genuine CLASSIFY-TIME collision (ENGINE.md's table), not the pre-write
    # re-fetch guard's drift case the other tests above already cover.
    store.rows[rid]["caption"] = "Remote caption"
    index2, state2, snapshot2 = _env(tmp_path)
    result = sync_fileset(
        tmp_path, store, adapter, FOLDER, index=index2, state=state2, snapshot=snapshot2,
        doc_bodies={doc_path: "# F\n\n![A different local caption](evidence/shot.png)\n"},
    )

    assert [p.outcome for p in result.plans] == [Outcome.COLLISION]
    sidecar = tmp_path / FOLDER / sidecar_path(PurePosixPath("shot.png")).name
    assert sidecar.name == "shot.remote.png"
    assert sidecar.exists()
    assert sidecar.read_bytes() == store.bodies[rid]  # the REMOTE version's bytes
    assert (tmp_path / FOLDER / "shot.png").read_bytes() == b"v1"  # local untouched
    assert store.rows[rid]["caption"] == "Remote caption"  # never silently overwritten

    # the user resolves it: the local caption opinion is brought in line with the
    # remote's current value.
    index3, state3, snapshot3 = _env(tmp_path)
    result2 = sync_fileset(
        tmp_path, store, adapter, FOLDER, index=index3, state=state3, snapshot=snapshot3,
        doc_bodies={doc_path: "# F\n\n![Remote caption](evidence/shot.png)\n"},
    )

    assert all(p.outcome is not Outcome.COLLISION for p in result2.plans)
    assert not sidecar.exists()  # cleared once no longer in collision
    # and the now-cleared sidecar was never itself mistaken for a new local file
    assert not any(p.path is not None and p.path.name == "shot.remote.png"
                   for p in result2.plans)
