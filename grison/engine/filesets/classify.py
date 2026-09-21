from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from grison.engine.classify import classify
from grison.engine.identity import Missing, Unindexed, pair
from grison.engine.model import Outcome, Plan, RemoteRecord
from grison.engine.state import StateStore
from grison.hashing import digest

from .captions import ReferenceCaption
from .model import FileSetAdapter, RunOptions, _cached_body_hash, _canonical, _hash_bytes


def _dedupe_path(path: PurePosixPath, claimed_names: set[str]) -> PurePosixPath:
    """``claimed_names`` is every filename already spoken for IN THIS FOLDER — disk,
    index, or a sibling PULL_NEW plan already assigned earlier in the same loop (see
    the running ``claimed_names`` set built in :func:`sync_fileset`, which is what
    makes two same-named remote rows in one sync dedupe against EACH OTHER, not just
    against pre-sync state)."""
    if path.name not in claimed_names:
        return path
    stem, suffix = PurePosixPath(path.name).stem, PurePosixPath(path.name).suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if candidate.name not in claimed_names:
            return candidate
        n += 1


def _rows_after(
    remote_rows: dict[int, RemoteRecord], plans: list[Plan]
) -> dict[str, dict[str, Any]]:
    """Filename -> latest known {caption, description, ...} after this run's
    writes — what every referencing document's alt should read afterwards."""
    out: dict[str, dict[str, Any]] = {
        row.data["filename"]: row.data for row in remote_rows.values()
    }
    for p in plans:
        if p.outcome is Outcome.DELETE_REMOTE and p.remote is not None:
            out.pop(p.remote.data["filename"], None)
        elif p.remote is not None:
            out[p.remote.data["filename"]] = p.remote.data
    return out


def _pairing_plans(
    kind: str,
    folder: PurePosixPath,
    missing_names: set[str],
    unindexed_names: set[str],
    indexed: dict[PurePosixPath, int],
    state: StateStore,
    local_files: dict[str, bytes],
) -> tuple[list[Plan], set[str], set[str]]:
    """Move pairing: identical bytes only (D1 — changed bytes under the same
    name is a new row, never a move+edit — see module docstring). Both sides of
    the "identical" comparison are RAW BYTES hashes (``_hash_bytes``, the same
    hash space ``_cached_body_hash``/``Unindexed.content_hash`` use) — comparing
    a bytes hash against the canonical (body+caption+description) digest used
    elsewhere for ``state.base`` would never match even for byte-identical
    content (different hash spaces, so "identical" could never fire), which is
    why renaming a file used to always fall through to CREATE+DELETE_REMOTE
    instead of pairing as a MOVE."""
    missing: list[Missing] = []
    for name in missing_names:
        path = folder / name
        rid = indexed[path]
        missing.append(
            Missing(
                path=path,
                id=rid,
                base_hash=_cached_body_hash(state, kind, rid),
                remote_content=None,
            )
        )
    unindexed: list[Unindexed] = []
    for name in unindexed_names:
        unindexed.append(
            Unindexed(path=folder / name, content="", content_hash=_hash_bytes(local_files[name]))
        )
    result = pair(missing, unindexed)
    plans = [
        Plan(kind=kind, outcome=Outcome.MOVE, path=d.new_path, id=d.id, move_from=d.old_path)
        for d in result.decisions
    ]
    paired_missing = {d.old_path.name for d in result.decisions}
    paired_unindexed = {d.new_path.name for d in result.decisions}
    return plans, paired_missing, paired_unindexed


def _wrap_remote(row: RemoteRecord | None, canon_hash: str | None) -> RemoteRecord | None:
    """Carry classification's own canonical hash on the ``Plan.remote`` it hands to
    ``_apply_*`` — ``cached_hash`` is exactly the field
    :func:`grison.engine.common.refetch_guard` (via this module's own ``_refetch_guard``
    below) compares a fresh re-fetch against, the same idiom
    :func:`grison.engine.documents.model._remote_hash` uses for a document adapter's
    skip-detail-fetch placeholder."""
    if row is None:
        return None
    return RemoteRecord(
        id=row.id, data=row.data, witness=row.witness, cached_hash=canon_hash, losses=row.losses
    )


def _classify_one(  # noqa: PLR0913
    adapter: FileSetAdapter,
    ctx: Any,
    path: PurePosixPath,
    rid: int,
    body: bytes,
    remote_rows: dict[int, RemoteRecord],
    state: StateStore,
    options: RunOptions,
    captions: dict[str, ReferenceCaption],
) -> Plan:
    kind = adapter.kind
    row = remote_rows.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    local_hash = _hash_bytes(body)

    remote_body_hash: str | None = None
    remote_canon_hash: str | None = None
    if row is not None:
        # D1: bytes are immutable for a given id, so a cached digest never goes
        # stale — a clean sync (the common case) never downloads this file's
        # body at all. A record with no cache yet pays for one download, which
        # immediately reseeds state (CLASSIFY, not just apply, since a CLEAN
        # outcome never reaches _dispatch's own state.put) so every later sync
        # is warm too, not just the ones that happen to write something.
        cached = st.witness.get("body_hash") if st else None
        remote_body_hash = cached if isinstance(cached, str) else None
        if remote_body_hash is None:
            remote_body_hash = _hash_bytes(adapter.fetch_body(ctx, rid))
            if not options.dry_run:
                state.put(
                    kind,
                    rid,
                    base=base_hash,
                    witness={**(st.witness if st else {}), "body_hash": remote_body_hash},
                )
        remote_canon_hash = digest(
            _canonical(
                body_hash=remote_body_hash,
                caption=row.data.get("caption", ""),
                description=row.data.get("description", ""),
                supports_caption=adapter.supports_caption,
            )
        )

    opinion = captions.get(path.name)
    if opinion is not None and opinion.has_opinion:
        local_caption, local_description = opinion.caption, opinion.description
    else:
        local_caption = row.data.get("caption", "") if row else ""
        local_description = row.data.get("description", "") if row else ""
    local_canon_hash = digest(
        _canonical(
            body_hash=local_hash,
            caption=local_caption,
            description=local_description,
            supports_caption=adapter.supports_caption,
        )
    )

    outcome = classify(
        indexed=True,
        local_present=True,
        remote_present=row is not None,
        local_hash=local_canon_hash,
        remote_hash=remote_canon_hash,
        base_hash=base_hash,
        force_local=path in options.force_local,
        force_remote=path in options.force_remote,
    )
    remote = _wrap_remote(row, remote_canon_hash)
    if outcome is Outcome.PUSH and remote_body_hash is not None and remote_body_hash != local_hash:
        # bytes changed under the same name — never a metadata-only update
        # (D1): re-upload as a new row instead (see _apply_reupload).
        return Plan(
            kind=kind,
            outcome=Outcome.MOVE_EDIT,
            path=path,
            id=rid,
            remote=remote,
            base_hash=base_hash,
            reason="bytes changed under the same name",
        )
    return Plan(kind=kind, outcome=outcome, path=path, id=rid, remote=remote, base_hash=base_hash)


def _classify_missing(
    adapter: FileSetAdapter,
    ctx: Any,
    path: PurePosixPath,
    rid: int,
    remote_rows: dict[int, RemoteRecord],
    state: StateStore,
    options: RunOptions,
) -> Plan:
    kind = adapter.kind
    row = remote_rows.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    remote_hash = None
    if row is not None:
        # Warm the body-hash cache exactly like `_classify_one` does on a miss
        # (same D1 reasoning: immutable-per-id bytes never need re-verifying once
        # cached) — a record only ever reached via THIS path (local file already
        # missing) used to leave the cache cold forever, which is what made the
        # pre-write re-fetch guard's own cache-miss fallback (`_refetch_guard`)
        # matter: without either half fixed, a cold-cache DELETE_REMOTE always
        # false-COLLIDED.
        cached = st.witness.get("body_hash") if st else None
        if isinstance(cached, str):
            body_hash = cached
        else:
            body_hash = _hash_bytes(adapter.fetch_body(ctx, rid))
            if not options.dry_run:
                state.put(
                    kind,
                    rid,
                    base=base_hash,
                    witness={**(st.witness if st else {}), "body_hash": body_hash},
                )
        remote_hash = digest(
            _canonical(
                body_hash=body_hash,
                caption=row.data.get("caption", ""),
                description=row.data.get("description", ""),
                supports_caption=adapter.supports_caption,
            )
        )
    outcome = classify(
        indexed=True,
        local_present=False,
        remote_present=row is not None,
        local_hash=None,
        remote_hash=remote_hash,
        base_hash=base_hash,
        force_local=path in options.force_local,
        force_remote=path in options.force_remote,
    )
    return Plan(
        kind=kind,
        outcome=outcome,
        path=path,
        id=rid,
        remote=_wrap_remote(row, remote_hash),
        base_hash=base_hash,
    )
