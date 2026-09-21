from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.classify import classify
from grison.engine.identity import Missing, PairDecision, Unindexed, pair
from grison.engine.model import LocalDoc, Outcome, Plan, RemoteRecord
from grison.engine.state import StateStore
from grison.hashing import digest

from .model import RunOptions, _remote_hash


def _pairing_plans(  # noqa: PLR0913
    adapter: Adapter,
    ctx: Any,
    root: Path,
    missing_paths: set[PurePosixPath],
    unindexed_paths: set[PurePosixPath],
    indexed: dict[PurePosixPath, int],
    local_docs: dict[PurePosixPath, LocalDoc],
    remote_records: dict[int, RemoteRecord],
    state: StateStore,
) -> list[Plan]:
    kind = adapter.kind
    missing: list[Missing] = []
    for p in missing_paths:
        rid = indexed[p]
        st = state.get(kind, rid)
        remote = remote_records.get(rid)
        if remote is not None and remote.cached_hash is not None:
            # a skip-detail-fetch placeholder (ENGINE.md skip-fetch fast path) has no
            # real content to compare against for identity pairing — this path is
            # only reached when the file that USED to be here is actually missing
            # (a real move/delete happened), so paying for one real fetch here is
            # both rare and necessary; replace the cached entry so every later use
            # in this run (canonical_remote, render_local, …) sees full data too.
            fresh = adapter.refetch(ctx, rid)
            if fresh is not None:
                remote_records[rid] = fresh
            # a None refetch means the record vanished between the bulk fetch and
            # now: pair against nothing rather than against the placeholder's stub
            # data, which is not real content.
            remote = fresh
        remote_text = adapter.render_local(remote.data, path=p) if remote is not None else None
        missing.append(
            Missing(path=p, id=rid, base_hash=st.base if st else None, remote_content=remote_text)
        )
    unindexed: list[Unindexed] = []
    for p in unindexed_paths:
        doc = local_docs[p]
        h = digest(adapter.canonical_local(doc.doc))
        unindexed.append(Unindexed(path=p, content=doc.raw_text, content_hash=h))

    result = pair(missing, unindexed)
    plans: list[Plan] = []
    for d in result.decisions:
        plans.append(_pair_decision_to_plan(adapter, d, local_docs, remote_records))
    return plans


def _pair_decision_to_plan(
    adapter: Adapter,
    d: PairDecision,
    local_docs: dict[PurePosixPath, LocalDoc],
    remote_records: dict[int, RemoteRecord],
) -> Plan:
    kind = adapter.kind
    doc = local_docs[d.new_path]
    remote = remote_records.get(d.id)
    if not d.edited:
        # identical content: a remote write happens ONLY if the directory move
        # implies a different parent than the remote currently has (never a no-op
        # write) — apply_remote.py's ``_apply_move`` checks canonical equality to decide.
        outcome = Outcome.MOVE
    else:
        outcome = Outcome.MOVE_EDIT
    return Plan(
        kind=kind,
        outcome=outcome,
        path=d.new_path,
        id=d.id,
        local=doc,
        remote=remote,
        move_from=d.old_path,
    )


def _classify_matched(  # noqa: PLR0913
    adapter: Adapter,
    path: PurePosixPath,
    rid: int,
    local_docs: dict[PurePosixPath, LocalDoc],
    remote_records: dict[int, RemoteRecord],
    state: StateStore,
    options: RunOptions,
) -> Plan:
    kind = adapter.kind
    doc = local_docs[path]
    remote = remote_records.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    local_hash = digest(adapter.canonical_local(doc.doc))
    remote_hash = _remote_hash(adapter, remote)
    outcome = classify(
        indexed=True,
        local_present=True,
        remote_present=remote is not None,
        local_hash=local_hash,
        remote_hash=remote_hash,
        base_hash=base_hash,
        read_only=adapter.mode == "read-only",
        append_only=adapter.mode == "append-only",
        force_local=path in options.force_local,
        force_remote=path in options.force_remote,
    )
    return Plan(
        kind=kind, outcome=outcome, path=path, id=rid, local=doc, remote=remote, base_hash=base_hash
    )


def _classify_missing(
    adapter: Adapter,
    path: PurePosixPath,
    rid: int,
    remote_records: dict[int, RemoteRecord],
    state: StateStore,
    options: RunOptions,
) -> Plan:
    kind = adapter.kind
    remote = remote_records.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    remote_hash = _remote_hash(adapter, remote)
    outcome = classify(
        indexed=True,
        local_present=False,
        remote_present=remote is not None,
        local_hash=None,
        remote_hash=remote_hash,
        base_hash=base_hash,
        read_only=adapter.mode == "read-only",
        append_only=adapter.mode == "append-only",
        force_local=path in options.force_local,
        force_remote=path in options.force_remote,
    )
    return Plan(kind=kind, outcome=outcome, path=path, id=rid, remote=remote, base_hash=base_hash)
