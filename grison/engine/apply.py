"""The ONE apply loop (ENGINE.md 'The apply loop') for one adapter (one record kind)
per call. :func:`run` is what ``grison sync`` calls once per engine-managed kind (today
just ``bs.page``, ``bs.book``, ``bs.chapter``, ``bs.shelf`` — the wiki step); later
steps call it again for their own adapters, unchanged.

Order: classify (identity pairing first, then the table) -> validation gate ->
change guard -> apply (pre-write re-fetch guard, undo capture, per-record isolation) ->
bookkeeping (index + state, atomic per record) -> canonicalisation-after-push ->
collision sidecars.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import Adapter
from grison.engine.classify import classify
from grison.engine.events import build_event, emit_losses
from grison.engine.identity import Missing, PairDecision, Unindexed, pair
from grison.engine.model import (
    Event,
    KindSummary,
    LocalDoc,
    Outcome,
    Plan,
    RemoteRecord,
)
from grison.engine.sidecar import sidecar_path
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot, UndoOp
from grison.fsio import atomic_write_text
from grison.hashing import digest
from grison.index import Index, IndexKind
from grison.validator.registry import Failure

_MASS_CHANGE_MIN = 5
_MASS_CHANGE_RATIO = 0.2

# Outcomes whose apply step is a real remote write (a create/update/delete call) —
# these count toward the change guard's W, and are what the pre-write re-fetch guard
# and undo capture wrap.
_REMOTE_WRITE_OUTCOMES = frozenset(
    {Outcome.PUSH, Outcome.CREATE, Outcome.DELETE_REMOTE, Outcome.MOVE_EDIT}
)
# Outcomes whose apply step overwrites/removes something ALREADY local (ENGINE.md's
# change guard: "D = planned local overwrites/removals (PULL + DELETE_LOCAL)").
# PULL_NEW deliberately excluded: it creates a file that didn't exist before — there
# is nothing local to lose, so a first sync (or discovering a batch of brand-new
# remote records) is never itself withheld by the D-side guard.
_LOCAL_WRITE_OUTCOMES = frozenset({Outcome.PULL, Outcome.DELETE_LOCAL})


def _remote_hash(adapter: Adapter, remote: RemoteRecord | None) -> str | None:
    if remote is None:
        return None
    if remote.cached_hash is not None:
        return remote.cached_hash
    return digest(adapter.canonical_remote(remote.data))


@dataclass
class RunOptions:
    dry_run: bool = False
    force_local: frozenset[PurePosixPath] = frozenset()
    force_remote: frozenset[PurePosixPath] = frozenset()
    mass_change_ratio: float = _MASS_CHANGE_RATIO


def run(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: Adapter,
    *,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    failures: list[Failure],
    options: RunOptions | None = None,
) -> tuple[list[Plan], list[Event], KindSummary]:
    """Run the full classify+apply cycle for ``adapter``'s kind. Mutates ``index``
    in place (callers save it once after every adapter has run); ``snapshot`` collects
    undo ops across possibly-several adapter runs sharing one sync's snapshot dir.
    Returns the plans reached, the events emitted (in application order — callers
    render/print them, live or after the fact, via :mod:`grison.engine.events`), and
    this kind's summary."""
    options = options or RunOptions()
    kind = adapter.kind
    events: list[Event] = []
    summary = KindSummary(kind=kind)
    invalid_paths = {f.path for f in failures}

    local_docs: dict[PurePosixPath, LocalDoc] = {d.path: d for d in adapter.scan_local(root)}
    remote_records: dict[int, RemoteRecord] = adapter.fetch_remote(ctx)
    indexed = {
        PurePosixPath(p): rec.id for p, rec in index.records.items() if rec.kind.value == kind
    }
    indexed_ids = set(indexed.values())

    present_indexed = {p for p in local_docs if p in indexed}
    missing_paths = {p for p in indexed if p not in local_docs}
    unindexed_paths = {p for p in local_docs if p not in indexed}
    remote_unindexed = {i for i in remote_records if i not in indexed_ids}

    plans: list[Plan] = _pairing_plans(
        adapter, ctx, root, missing_paths, unindexed_paths, indexed, local_docs, remote_records,
        state,
    )
    paired_missing = {p.move_from for p in plans if p.move_from is not None}
    paired_unindexed = {p.path for p in plans if p.move_from is not None}

    for mpath in sorted(present_indexed, key=str):
        plans.append(
            _classify_matched(adapter, mpath, indexed[mpath], local_docs, remote_records, state,
                              options)
        )
    for mpath in sorted(missing_paths - paired_missing, key=str):
        plans.append(
            _classify_missing(adapter, mpath, indexed[mpath], remote_records, state, options)
        )
    for mpath in sorted(unindexed_paths - paired_unindexed, key=str):
        plans.append(Plan(kind=kind, outcome=Outcome.CREATE, path=mpath, local=local_docs[mpath]))
    for rid in sorted(remote_unindexed):
        plans.append(Plan(kind=kind, outcome=Outcome.PULL_NEW, id=rid, remote=remote_records[rid]))

    # veto (server-side condition the table can't express) and the validation gate —
    # both only ever turn a remote-write outcome into SKIP/INVALID; pulls proceed.
    for p in plans:
        if p.outcome in _REMOTE_WRITE_OUTCOMES and p.path is not None:
            if str(p.path) in invalid_paths:
                p.rule_ids = tuple(f.rule_id for f in failures if f.path == str(p.path))
                p.outcome = Outcome.INVALID
                continue
        if p.outcome not in (Outcome.CLEAN, Outcome.REPAIR, Outcome.FORGET) and (
            p.local is not None or p.remote is not None
        ):
            veto = adapter.veto(
                p.local.doc if p.local else None, p.remote.data if p.remote else None
            )
            if veto is not None:
                p.outcome = Outcome.SKIP
                p.reason = veto.reason
                p.severity = veto.severity

    _apply_change_guard(plans, options, summary)

    for p in plans:
        _apply_one(root, ctx, adapter, p, index, state, snapshot, events, options)
        summary.bump(p.outcome)
        if p.is_problem:
            label = str(p.path) if p.path is not None else (
                adapter.remote_label(p.remote.data) if p.remote is not None else str(p.id)
            )
            summary.problem_paths.append(label)

    if not options.dry_run:
        _clear_stale_sidecars(root, plans)
    return plans, events, summary


def _pairing_plans(  # noqa: PLR0913
    adapter: Adapter, ctx: Any, root: Path, missing_paths: set[PurePosixPath],
    unindexed_paths: set[PurePosixPath], indexed: dict[PurePosixPath, int],
    local_docs: dict[PurePosixPath, LocalDoc], remote_records: dict[int, RemoteRecord],
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
                remote = fresh
        remote_text = adapter.render_local(remote.data, path=p) if remote is not None else None
        missing.append(Missing(path=p, id=rid, base_hash=st.base if st else None,
                               remote_content=remote_text))
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
    adapter: Adapter, d: PairDecision, local_docs: dict[PurePosixPath, LocalDoc],
    remote_records: dict[int, RemoteRecord],
) -> Plan:
    kind = adapter.kind
    doc = local_docs[d.new_path]
    remote = remote_records.get(d.id)
    if not d.edited:
        # identical content: a remote write happens ONLY if the directory move
        # implies a different parent than the remote currently has (never a no-op
        # write) — apply.py's MOVE handler checks canonical equality to decide.
        outcome = Outcome.MOVE
    else:
        outcome = Outcome.MOVE_EDIT
    return Plan(
        kind=kind, outcome=outcome, path=d.new_path, id=d.id, local=doc, remote=remote,
        move_from=d.old_path,
    )


def _classify_matched(  # noqa: PLR0913
    adapter: Adapter, path: PurePosixPath, rid: int, local_docs: dict[PurePosixPath, LocalDoc],
    remote_records: dict[int, RemoteRecord], state: StateStore, options: RunOptions,
) -> Plan:
    kind = adapter.kind
    doc = local_docs[path]
    remote = remote_records.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    local_hash = digest(adapter.canonical_local(doc.doc))
    remote_hash = _remote_hash(adapter, remote)
    outcome = classify(
        indexed=True, local_present=True, remote_present=remote is not None,
        local_hash=local_hash, remote_hash=remote_hash, base_hash=base_hash,
        read_only=adapter.mode == "read-only", append_only=adapter.mode == "append-only",
        force_local=path in options.force_local, force_remote=path in options.force_remote,
    )
    return Plan(kind=kind, outcome=outcome, path=path, id=rid, local=doc, remote=remote,
                base_hash=base_hash)


def _classify_missing(
    adapter: Adapter, path: PurePosixPath, rid: int, remote_records: dict[int, RemoteRecord],
    state: StateStore, options: RunOptions,
) -> Plan:
    kind = adapter.kind
    remote = remote_records.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    remote_hash = _remote_hash(adapter, remote)
    outcome = classify(
        indexed=True, local_present=False, remote_present=remote is not None,
        local_hash=None, remote_hash=remote_hash, base_hash=base_hash,
        read_only=adapter.mode == "read-only", append_only=adapter.mode == "append-only",
        force_local=path in options.force_local, force_remote=path in options.force_remote,
    )
    return Plan(kind=kind, outcome=outcome, path=path, id=rid, remote=remote, base_hash=base_hash)


def _apply_change_guard(plans: list[Plan], options: RunOptions, summary: KindSummary) -> None:
    total = max(len(plans), 1)

    def _weight(p: Plan) -> int:
        return 2 if p.outcome in (Outcome.DELETE_REMOTE, Outcome.DELETE_LOCAL) else 1

    def _is_forced(p: Plan) -> bool:
        return p.path is not None and (p.path in options.force_local
                                       or p.path in options.force_remote)

    remote_writes = [p for p in plans if p.outcome in _REMOTE_WRITE_OUTCOMES and not _is_forced(p)]
    w = sum(_weight(p) for p in remote_writes)
    if w > _MASS_CHANGE_MIN and w > options.mass_change_ratio * total:
        for p in remote_writes:
            p.outcome = Outcome.WITHHELD

    local_writes = [p for p in plans if p.outcome in _LOCAL_WRITE_OUTCOMES and not _is_forced(p)]
    d = sum(_weight(p) for p in local_writes)
    if d > _MASS_CHANGE_MIN and d > options.mass_change_ratio * total:
        for p in local_writes:
            p.outcome = Outcome.WITHHELD


def _apply_one(  # noqa: PLR0912, PLR0913, PLR0915
    root: Path, ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
) -> None:
    try:
        _dispatch(root, ctx, adapter, p, index, state, snapshot, events, options)
    except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5)
        p.outcome = Outcome.FAILED
        p.reason = f"{type(e).__name__}: {e}"
        events.append(build_event("failed", p, adapter, detail=p.reason))


def _dispatch(  # noqa: PLR0912, PLR0913, PLR0915
    root: Path, ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
) -> None:
    kind = adapter.kind
    dry = options.dry_run

    if p.outcome is Outcome.CLEAN:
        return

    if p.outcome is Outcome.REPAIR:
        if not dry and p.id is not None:
            wit = state.get(kind, p.id)
            local_hash = digest(adapter.canonical_local(p.local.doc)) if p.local else None
            state.put(kind, p.id, base=local_hash, witness=wit.witness if wit else {})
        events.append(Event(verb="repair", path=str(p.path), dry_run=dry))
        return

    if p.outcome is Outcome.FORGET:
        if not dry and p.path is not None and p.id is not None:
            index.remove(str(p.path))
            state.forget(kind, p.id)
        events.append(Event(verb="forget", path=str(p.path), dry_run=dry))
        return

    if p.outcome is Outcome.INVALID:
        events.append(Event(verb="invalid", path=str(p.path), detail=", ".join(p.rule_ids)))
        return

    if p.outcome is Outcome.WITHHELD:
        events.append(Event(verb="withheld", path=str(p.path) if p.path else None))
        return

    if p.outcome is Outcome.SKIP:
        events.append(build_event("skip", p, adapter, detail=p.reason))
        return

    if p.outcome is Outcome.CREATE:
        _apply_create(root, ctx, adapter, p, index, state, snapshot, events, dry)
        return

    if p.outcome in (Outcome.PUSH, Outcome.MOVE_EDIT):
        _apply_update(root, ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome is Outcome.MOVE:
        _apply_move(root, ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome in (Outcome.PULL, Outcome.PULL_NEW):
        _apply_pull(root, adapter, p, index, state, snapshot, events, dry)
        return

    if p.outcome is Outcome.DELETE_REMOTE:
        _apply_delete_remote(ctx, adapter, p, index, state, snapshot, events, options)
        return

    if p.outcome is Outcome.DELETE_LOCAL:
        _apply_delete_local(root, p, index, state, snapshot, events, dry)
        return

    if p.outcome is Outcome.COLLISION:
        _apply_collision(root, adapter, p, events, dry)
        return


def _apply_create(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], dry: bool,
) -> None:
    assert p.local is not None and p.path is not None
    if dry:
        events.append(Event(verb="create", path=str(p.path), dry_run=True))
        return
    rec = adapter.create(ctx, p.local.doc)
    # crash-ordering (ENGINE.md §7): the index entry is written the instant the server
    # returns an id, before ANY other bookkeeping — a crash right after this line can
    # never produce a duplicate create on the next sync (see tests/test_engine_apply.py).
    index.set(str(p.path), IndexKind(adapter.kind), rec.id)
    # local_preimage: the author's own pre-create bytes — undoing this create restores
    # them (grison.engine.undo._replay_one), rather than deleting the file or leaving
    # it in its post-create mirrored form.
    snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=rec.id,
                           local_preimage=p.local.raw_text))
    text = adapter.render_local(rec.data, path=p.path)
    if text != p.local.raw_text:
        atomic_write_text(root / p.path, text)
    state.put(adapter.kind, rec.id, base=digest(adapter.canonical_remote(rec.data)),
              witness=rec.witness)
    emit_losses(events, str(p.path), rec.losses)
    events.append(Event(verb="create", path=str(p.path)))


def refetch_guard(
    *,
    refetch: Callable[[], RemoteRecord | None],
    expected_hash: str | None,
    canonical_hash: Callable[[RemoteRecord], str],
    forced: bool,
) -> tuple[RemoteRecord | None, bool]:
    """The ONE pre-write re-fetch guard (ENGINE.md §3), record-type-agnostic:
    re-fetch the one record about to be written and compare a canonical hash of
    what's there now against what classification saw. Returns ``(fresh,
    drifted)``; ``drifted`` True means the caller must treat the plan as a
    COLLISION instead of writing.

    Shared by :func:`grison.engine.apply._refetch_guard` (the document
    :class:`~grison.engine.adapter.Adapter` shape — ``refetch``/``canonical_remote``
    take a bare id/dict) and :mod:`grison.engine.filesets` (whose
    :class:`~grison.engine.filesets.FileSetAdapter` has a different ``refetch``
    signature and no ``canonical_remote`` at all — a body's bytes can never
    silently change under an id, so its own canonical hash only needs fresh
    metadata, not a re-download): each passes in its own way to refetch one
    record and hash it, and gets back the exact same comparison semantics —
    including ``expected_hash is None`` (nothing to compare against, e.g. no
    remote record at classification time) always meaning "not drifted"."""
    fresh = refetch()
    if fresh is None:
        return None, not forced
    if expected_hash is not None and not forced:
        if expected_hash != canonical_hash(fresh):
            return fresh, True
    return fresh, False


def _refetch_guard(
    ctx: Any, adapter: Adapter, p: Plan, options: RunOptions,
) -> tuple[RemoteRecord | None, bool]:
    """The document-adapter binding of :func:`refetch_guard`: ``p.remote`` may be a
    skip-detail-fetch placeholder (a small sentinel dict, missing most fields — see
    the adapter's own ``fetch_remote``), which would never equal a freshly fetched
    full record even when nothing actually changed — ``_remote_hash`` already knows
    to read that placeholder's ``cached_hash`` instead of hashing its sentinel
    content."""
    if p.id is None:
        return None, False
    rid = p.id
    forced = p.path is not None and p.path in options.force_local
    return refetch_guard(
        refetch=lambda: adapter.refetch(ctx, rid),
        expected_hash=_remote_hash(adapter, p.remote) if p.remote is not None else None,
        canonical_hash=lambda fresh: digest(adapter.canonical_remote(fresh.data)),
        forced=forced,
    )


def _apply_update(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
) -> None:
    assert p.local is not None and p.id is not None and p.path is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, options)
    if drifted:
        _write_collision_sidecar(root, adapter, p, fresh)
        p.outcome = Outcome.COLLISION
        events.append(Event(verb="collision", path=str(p.path),
                            detail="changed on the server since classification"))
        return
    # the wysiwyg/draft/recycle-bin guard is re-checked here, against the FRESH
    # pre-write data, not just the bulk-fetch snapshot classify() used — a page can
    # flip editor/draft state between classification and this write.
    veto = adapter.veto(p.local.doc if p.local else None, fresh.data if fresh else None)
    if veto is not None:
        p.outcome = Outcome.SKIP
        p.reason = veto.reason
        p.severity = veto.severity
        events.append(build_event("skip", p, adapter, detail=veto.reason))
        return
    if dry:
        verb = "move" if p.outcome is Outcome.MOVE_EDIT else "push"
        detail = f"from {p.move_from}" if p.move_from else ""
        events.append(Event(verb=verb, path=str(p.path), detail=detail, dry_run=True))
        return
    if fresh is None:
        # force-local on a "edited locally, deleted remotely" collision (ENGINE.md /
        # BRIEF: "re-create remotely") — the id classify() knew is gone; a PUSH here
        # means CREATE a new record and re-point the index at it, not update() an id
        # that no longer exists.
        rec = adapter.create(ctx, p.local.doc)
        old_id = p.id
        index.set(str(p.path), IndexKind(adapter.kind), rec.id)
        state.forget(adapter.kind, old_id)
        snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=rec.id,
                               local_preimage=p.local.raw_text))
        text = adapter.render_local(rec.data, path=p.path)
        if text != p.local.raw_text:
            atomic_write_text(root / p.path, text)
        state.put(adapter.kind, rec.id, base=digest(adapter.canonical_remote(rec.data)),
                  witness=rec.witness)
        emit_losses(events, str(p.path), rec.losses)
        events.append(Event(verb="push", path=str(p.path), detail="re-created remotely"))
        return
    preimage = fresh.data
    # The LOCAL half of a push undo (item 3, fix-findings): NOT `p.local.raw_text`
    # (that's the author's ABOUT-TO-BE-PUSHED text — the edit undo is supposed to
    # revert, not what it should restore) — the file as it would read if it
    # mirrored the OLD, pre-push remote state, exactly like a genuine pull of
    # `preimage` would have written it. Computed once, here, rather than lazily
    # at replay time, so undo never needs to re-derive a rendering decision the
    # forward loop already made once (and the undo module stays adapter-agnostic
    # about whether ANY given "push" op even has local content to restore — a
    # file-set caption push's `local_preimage` stays `None`, on purpose: a
    # caption/description change never touches the evidence file's own bytes).
    local_preimage = adapter.render_local(preimage, path=p.path)
    op = UndoOp(kind=adapter.kind, outcome=p.outcome.value, path=str(p.path),
               id=p.id, remote_preimage=preimage, move_from=str(p.move_from)
               if p.move_from else None, local_preimage=local_preimage)
    snapshot.record(op)
    resp = adapter.update(ctx, p.id, p.local.doc)
    if p.move_from is not None:
        index.move(str(p.move_from), str(p.path))
    text = adapter.render_local(resp.data, path=p.path)
    if text != p.local.raw_text:
        atomic_write_text(root / p.path, text)
    # ENGINE.md §6's canonicalisation-after-push hash IS the post-write canonical
    # hash a "push"/"move_edit" undo's re-fetch guard compares against (item 4) —
    # one computation, two uses, so the two can never quietly disagree.
    resp_hash = digest(adapter.canonical_remote(resp.data))
    op.post_write_hash = resp_hash
    state.put(adapter.kind, p.id, base=resp_hash, witness=resp.witness)
    emit_losses(events, str(p.path), resp.losses)
    verb = "move" if p.outcome is Outcome.MOVE_EDIT else "push"
    detail = f"from {p.move_from}" if p.move_from else ""
    events.append(Event(verb=verb, path=str(p.path), detail=detail))


def _apply_move(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
) -> None:
    """Identical content: index-only bookkeeping UNLESS the directory move implies a
    different remote parent — in which case exactly one write happens (never a no-op
    PUT for a plain rename)."""
    assert p.local is not None and p.id is not None and p.move_from is not None
    remote = p.remote
    needs_write = remote is not None and (
        adapter.canonical_local(p.local.doc) != adapter.canonical_remote(remote.data)
    )
    if options.dry_run:
        events.append(Event(verb="move", path=str(p.path), detail=f"from {p.move_from}",
                            dry_run=True))
        return
    index.move(str(p.move_from), str(p.path))
    if needs_write:
        p.outcome = Outcome.MOVE_EDIT  # reuse the update path for the reparent write
        _apply_update(root, ctx, adapter, p, index, state, snapshot, events, options)
        return
    events.append(Event(verb="move", path=str(p.path), detail=f"from {p.move_from}"))


def _apply_pull(  # noqa: PLR0913
    root: Path, adapter: Adapter, p: Plan, index: Index, state: StateStore, snapshot: Snapshot,
    events: list[Event], dry: bool,
) -> None:
    assert p.remote is not None
    path: PurePosixPath
    relocated_from: PurePosixPath | None = None
    if p.path is None:
        path = adapter.default_path(p.remote.data, root=root)
        existing = {PurePosixPath(rp) for rp in index.records}
        path = _dedupe(path, existing, root)
    else:
        # PULL-side relocation: the record's remote parent may have changed (a page
        # moved to another chapter on the server) — the file must physically move
        # too, or the next sync would see the (now stale) directory disagree with
        # the fresh content and push it straight back. Never a silent overwrite:
        # this only fires for an already-indexed record's own path.
        path = adapter.relocated_path(p.remote.data, current=p.path)
        if path != p.path:
            relocated_from = p.path
    text = adapter.render_local(p.remote.data, path=path)
    target = root / path
    existed = target.exists()
    if relocated_from is not None and existed:
        # never a silent overwrite: an unrelated file already occupies the
        # relocation target — surfaced as a FAILED outcome (per-record isolation),
        # the old file left exactly where it was, for the owner to resolve by hand.
        raise RuntimeError(f"relocation target exists: {path} — resolve manually")
    detail = f"from {relocated_from}" if relocated_from is not None else ""
    if dry:
        events.append(Event(verb="pull", path=str(path), detail=detail, dry_run=True))
        return
    # No undo entry: PULL/PULL_NEW is not a remote write (ENGINE.md 'Undo capture' —
    # "undo is for remote writes"; grison.engine.undo's own module docstring). The
    # local, git-tracked tree already has its own history for this.
    atomic_write_text(target, text)
    if relocated_from is not None:
        (root / relocated_from).unlink(missing_ok=True)
        index.move(str(relocated_from), str(path))
    else:
        index.set(str(path), IndexKind(adapter.kind), p.remote.id)
    state.put(adapter.kind, p.remote.id, base=digest(adapter.canonical_remote(p.remote.data)),
              witness=p.remote.witness)
    emit_losses(events, str(path), p.remote.losses)
    events.append(Event(verb="pull", path=str(path), detail=detail))


def _dedupe(path: PurePosixPath, existing: set[PurePosixPath], root: Path) -> PurePosixPath:
    if path not in existing and not (root / path).exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if candidate not in existing and not (root / candidate).exists():
            return candidate
        n += 1


def _apply_delete_remote(  # noqa: PLR0913
    ctx: Any, adapter: Adapter, p: Plan, index: Index, state: StateStore, snapshot: Snapshot,
    events: list[Event], options: RunOptions,
) -> None:
    assert p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, options)
    if drifted:
        p.outcome = Outcome.COLLISION
        events.append(Event(verb="collision", path=str(p.path),
                            detail="changed on the server since classification"))
        return
    if dry:
        events.append(Event(verb="delete-remote", path=str(p.path), dry_run=True))
        return
    preimage = fresh.data if fresh is not None else None
    snapshot.record(UndoOp(kind=adapter.kind, outcome="delete_remote", path=str(p.path),
                           id=p.id, remote_preimage=preimage))
    adapter.delete(ctx, p.id)
    if p.path is not None:
        index.remove(str(p.path))
    state.forget(adapter.kind, p.id)
    events.append(Event(verb="delete-remote", path=str(p.path)))


def _apply_delete_local(
    root: Path, p: Plan, index: Index, state: StateStore, snapshot: Snapshot,
    events: list[Event], dry: bool,
) -> None:
    assert p.path is not None and p.id is not None
    del snapshot  # not a remote write — no undo entry (see grison.engine.undo's docstring)
    target = root / p.path
    if dry:
        events.append(Event(verb="delete-local", path=str(p.path), dry_run=True))
        return
    target.unlink(missing_ok=True)
    index.remove(str(p.path))
    state.forget(p.kind, p.id)
    events.append(Event(verb="delete-local", path=str(p.path)))


def _write_collision_sidecar(
    root: Path, adapter: Adapter, p: Plan, remote: RemoteRecord | None,
) -> None:
    if p.path is None or remote is None:
        return
    text = adapter.render_local(remote.data, path=p.path)
    atomic_write_text(root / sidecar_path(p.path), text)


def _apply_collision(
    root: Path, adapter: Adapter, p: Plan, events: list[Event], dry: bool,
) -> None:
    if dry:
        events.append(Event(verb="collision", path=str(p.path), dry_run=True))
        return
    _write_collision_sidecar(root, adapter, p, p.remote)
    events.append(Event(verb="collision", path=str(p.path)))


def _clear_stale_sidecars(root: Path, plans: list[Plan]) -> None:
    """ENGINE.md §8: a sidecar is cleared as soon as its record is no longer in
    collision (resolved by force flag, or the two sides converged)."""
    for p in plans:
        if p.path is None or p.outcome is Outcome.COLLISION:
            continue
        sidecar = root / sidecar_path(p.path)
        sidecar.unlink(missing_ok=True)
