"""Helpers shared, verbatim, by both reconcile engines (:mod:`grison.engine.documents`
and :mod:`grison.engine.filesets`) — ENGINE.md's "one change guard", "one pre-write
re-fetch guard", "one validation gate" were already common before this module existed
(declared in what was then a single ``grison.engine.apply``, imported from
:mod:`grison.engine.filesets`); this is where they actually belong now that neither
engine is the other's host module.

Round 2 dedup: every function here is IDENTICAL between the two engines — same
behaviour, same event wording, same exit semantics — not merely similar. Where the
two engines differ even slightly (the classification TABLE's read-only/append-only
handling; the file-set re-upload rule; how CREATE/PUSH/PULL/MOVE are actually
written), that logic stays in :mod:`grison.engine.documents`/:mod:`grison.engine.
filesets` respectively — see each module's own docstring for why it wasn't folded
in here too.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from grison.engine.model import Event, KindSummary, Outcome, Plan, RemoteRecord
from grison.engine.state import StateStore
from grison.index import Index
from grison.validator.registry import Failure

#: The ONE change-guard's threshold + outcome classification (ENGINE.md 'Apply
#: loop' item 2).
MASS_CHANGE_MIN = 5
MASS_CHANGE_RATIO = 0.2

# Outcomes whose apply step is a real remote write (a create/update/delete call) —
# these count toward the change guard's W, and are what the pre-write re-fetch guard
# and undo capture wrap.
REMOTE_WRITE_OUTCOMES = frozenset(
    {Outcome.PUSH, Outcome.CREATE, Outcome.DELETE_REMOTE, Outcome.MOVE_EDIT}
)
# Outcomes whose apply step overwrites/removes something ALREADY local (ENGINE.md's
# change guard: "D = planned local overwrites/removals (PULL + DELETE_LOCAL)").
# PULL_NEW deliberately excluded: it creates a file that didn't exist before — there
# is nothing local to lose, so a first sync (or discovering a batch of brand-new
# remote records) is never itself withheld by the D-side guard.
LOCAL_WRITE_OUTCOMES = frozenset({Outcome.PULL, Outcome.DELETE_LOCAL})


def change_guard(plans: list[Plan], options: Any) -> None:
    """The ONE change-guard rule (ENGINE.md 'Apply loop' item 2, "one change guard
    with one threshold"): mutates ``plans`` in place, flipping every unforced
    remote-write outcome to :data:`~grison.engine.model.Outcome.WITHHELD` when W
    (the weighted remote-write count) trips the threshold, independently doing the
    same for D (the weighted local-write count). ``options`` only needs
    ``force_local``/``force_remote``/``mass_change_ratio``/``allow_mass_change`` —
    :class:`grison.engine.documents.RunOptions` and :class:`grison.engine.filesets.
    RunOptions` (two differently-shaped dataclasses) both satisfy that by duck
    typing, which is how each engine shares this one implementation instead of
    carrying its own copy."""
    if getattr(options, "allow_mass_change", False):
        return
    total = max(len(plans), 1)

    def _weight(p: Plan) -> int:
        return 2 if p.outcome in (Outcome.DELETE_REMOTE, Outcome.DELETE_LOCAL) else 1

    def _is_forced(p: Plan) -> bool:
        return p.path is not None and (
            p.path in options.force_local or p.path in options.force_remote
        )

    remote_writes = [p for p in plans if p.outcome in REMOTE_WRITE_OUTCOMES and not _is_forced(p)]
    w = sum(_weight(p) for p in remote_writes)
    if w > MASS_CHANGE_MIN and w > options.mass_change_ratio * total:
        for p in remote_writes:
            p.outcome = Outcome.WITHHELD

    local_writes = [p for p in plans if p.outcome in LOCAL_WRITE_OUTCOMES and not _is_forced(p)]
    d = sum(_weight(p) for p in local_writes)
    if d > MASS_CHANGE_MIN and d > options.mass_change_ratio * total:
        for p in local_writes:
            p.outcome = Outcome.WITHHELD


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

    Shared by :mod:`grison.engine.documents` (the document
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


def validation_gate(plans: list[Plan], failures: list[Failure]) -> None:
    """The ONE validation gate (ENGINE.md 'Apply loop' item 1, "a document with
    failures is never pushed or created", extended to every record type): mutates
    ``plans`` in place, turning any :data:`REMOTE_WRITE_OUTCOMES` plan whose path
    already failed `grison validate` into :data:`~grison.engine.model.Outcome.
    INVALID`, naming the rule id(s) that fired. A pull/delete-local/etc proceeds
    regardless — this only ever turns a remote WRITE into a no-write.

    Shared verbatim by :func:`grison.engine.documents.run` and
    :func:`grison.engine.filesets.sync_fileset`; each engine's own veto step (a
    server-side condition the table can't express — documents only, file sets have
    none) runs separately, after this gate."""
    invalid_paths = {f.path for f in failures}
    for p in plans:
        if p.outcome in REMOTE_WRITE_OUTCOMES and p.path is not None:
            if str(p.path) in invalid_paths:
                p.rule_ids = tuple(f.rule_id for f in failures if f.path == str(p.path))
                p.outcome = Outcome.INVALID


K = TypeVar("K", bound=Hashable)


def indexed_for_kind(
    index: Index, kind: str, *, under: PurePosixPath | None = None
) -> dict[PurePosixPath, int]:
    """Every index entry of ``kind``, path -> remote id — optionally restricted to
    paths directly ``under`` one folder (the file-set engine's own per-scope index
    slice; the document engine wants the whole kind, so it omits ``under``)."""
    return {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind.value == kind and (under is None or PurePosixPath(p).parent == under)
    }


def partition_present(
    local_keys: Iterable[K], indexed_keys: Iterable[K]
) -> tuple[set[K], set[K], set[K]]:
    """The ONE "present / missing / unindexed" derivation (ENGINE.md 'Identity'):
    given this run's local keys (a document's path, a file-set member's filename)
    and this kind's indexed keys, returns ``(present, missing, unindexed)`` —
    present-and-indexed, indexed-but-missing-locally ("M"), and present-but-
    unindexed ("U")."""
    local_set = set(local_keys)
    indexed_set = set(indexed_keys)
    return local_set & indexed_set, indexed_set - local_set, local_set - indexed_set


def dedupe_path(path: PurePosixPath, taken: Callable[[PurePosixPath], bool]) -> PurePosixPath:
    """The ONE "name already spoken for" loop: append ``-2``, ``-3``, … before the
    suffix until ``taken`` (the caller's own idea of "already spoken for" — disk +
    index for a document's default pull path; a running claimed-names set for a
    file-set member) says no. Callers differ only in what ``taken`` checks, never
    in the loop itself."""
    if not taken(path):
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not taken(candidate):
            return candidate
        n += 1


def run_apply_loop(
    plans: list[Plan],
    summary: KindSummary,
    apply_one: Callable[[Plan], None],
    problem_label: Callable[[Plan], str],
) -> None:
    """The ONE apply-loop shell: apply each plan (per-record isolation is
    ``apply_one``'s own job — both engines' ``_apply_one`` already try/except
    around ``_dispatch``), bump the kind summary, and record a problem path's
    label. ``problem_label`` lets each engine pick its own fallback when a
    problem plan has no local path (a document falls back to ``str(p.id)``; a
    file set falls back to the adapter's remote label)."""
    for p in plans:
        apply_one(p)
        summary.bump(p.outcome)
        if p.is_problem:
            summary.problem_paths.append(problem_label(p))


def apply_delete_local(
    root: Path,
    p: Plan,
    index: Index,
    state: StateStore,
    events: list[Event],
    dry: bool,
) -> None:
    """The ONE DELETE_LOCAL apply step: the remote row is gone and the local copy
    still matches the last-synced base (classify.py's ordinary DELETE_LOCAL row,
    for either engine) — remove the file and forget its state. No undo entry:
    DELETE_LOCAL is not a remote write (:mod:`grison.engine.undo`'s own module
    docstring — "undo is for remote writes") — the local, git-tracked tree already
    has its own history for this."""
    assert p.path is not None and p.id is not None
    if dry:
        events.append(Event(verb="delete-local", path=str(p.path), dry_run=True))
        return
    (root / p.path).unlink(missing_ok=True)
    index.remove(str(p.path))
    state.forget(p.kind, p.id)
    events.append(Event(verb="delete-local", path=str(p.path)))
