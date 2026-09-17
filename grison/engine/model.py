"""Core data types the sync engine and every adapter share (ENGINE.md).

Deliberately record-type-agnostic: nothing here names a Ghostwriter or BookStack
concept, or even "page"/"finding" — a *kind* is just an opaque string handed in by the
adapter (an :class:`grison.index.IndexKind` value in practice, but this package never
imports that enum, so it stays free of the two remotes' vocabulary; see
``tests/test_engine_no_leak.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

#: Anything ``json.dumps`` can hash by way of :func:`grison.hashing.digest` — the
#: output of an adapter's ``canonical_local``/``canonical_remote``.
Canonical = Any


class Outcome(StrEnum):
    """Every result classify()/apply() can land a record in — the closed set the
    events/result/exit-code policy (ENGINE.md §10) is built on. String-valued so it
    serializes directly for ``--json``."""

    CLEAN = "clean"
    REPAIR = "repair"
    PUSH = "push"
    PULL = "pull"
    PULL_NEW = "pull_new"
    CREATE = "create"
    COLLISION = "collision"
    DELETE_REMOTE = "delete_remote"
    DELETE_LOCAL = "delete_local"
    FORGET = "forget"
    MOVE = "move"  # identical content, index-only (or a pure reparent — never a no-op PUT)
    MOVE_EDIT = "move_edit"  # moved AND content changed — same remote id, updated in place
    INVALID = "invalid"
    WITHHELD = "withheld"
    SKIP = "skip"
    FAILED = "failed"


#: Outcomes that mean "nothing to do" once reached (never applied, never reported as a
#: problem by the exit-code policy).
QUIET_OUTCOMES = frozenset({Outcome.CLEAN})

#: Outcomes ENGINE.md §10 says make a run's exit code nonzero.
PROBLEM_OUTCOMES = frozenset(
    {Outcome.INVALID, Outcome.COLLISION, Outcome.FAILED, Outcome.WITHHELD}
)


@dataclass(frozen=True)
class LocalDoc:
    """One local file for one record. ``doc`` is the adapter's own parsed-document
    object (opaque to the engine); ``raw_text`` is the exact on-disk bytes (decoded),
    used for undo pre-images and canonicalisation-after-push rewrites."""

    path: PurePosixPath
    doc: Any
    raw_text: str


@dataclass(frozen=True)
class RemoteRecord:
    """One remote record as fetched (bulk ``fetch_remote`` or single ``refetch``).
    ``data`` is the adapter's own record shape (opaque to the engine). ``witness`` is
    whatever cheap server-side change marker the adapter has (state.py's ``witness``
    field) — compared only as an opaque dict, by the adapter, never interpreted here."""

    id: int
    data: Any
    witness: dict[str, Any] = field(default_factory=dict)
    cached_hash: str | None = None
    """An adapter may set this when it skipped fetching full detail because a cheap
    witness (e.g. BookStack's ``updated_at``/``revision_count``) proves content is
    unchanged since the last sync — the engine uses it in place of
    ``digest(canonical_remote(data))`` so a provably-clean record costs zero detail
    fetches. ``None`` (the default) means "hash it normally"."""


@dataclass
class RecordInput:
    """Everything :func:`grison.engine.classify.classify` needs for one record slot:
    what's on disk (or not), what's on the server (or not), and the merge base from
    state. Never mutated by classify() — apply.py builds one of these per record after
    identity resolution (:mod:`grison.engine.identity`) has already turned any
    move/move+edit pair into its own :class:`Plan`, so a ``RecordInput`` only ever
    describes an ordinary indexed record, a brand-new local file, or a brand-new
    remote record classify's table calls CREATE / PULL_NEW."""

    kind: str
    path: PurePosixPath | None
    id: int | None
    local: LocalDoc | None
    remote: RemoteRecord | None
    local_hash: str | None
    remote_hash: str | None
    base_hash: str | None
    indexed: bool
    read_only: bool = False
    append_only: bool = False
    force_local: bool = False
    force_remote: bool = False


@dataclass
class Plan:
    """classify()'s (or identity's move-pairing's) verdict for one record, with enough
    context for apply() to act on it and for events/results to describe it."""

    kind: str
    outcome: Outcome
    path: PurePosixPath | None = None
    id: int | None = None
    local: LocalDoc | None = None
    remote: RemoteRecord | None = None
    base_hash: str | None = None
    reason: str = ""
    move_from: PurePosixPath | None = None
    forced: bool = False
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Event:
    """One line of sync output (ENGINE.md 'Events'): ``<verb> <path>[ — detail]``,
    verb from the closed list in :mod:`grison.engine.events`. No arrow glyphs — a move
    says "from <old>" in its detail, in words. ``dry_run`` controls the renderer's
    "would " prefix; it is not baked into ``verb``/``detail`` themselves."""

    verb: str
    path: str | None
    detail: str = ""
    dry_run: bool = False


@dataclass
class KindSummary:
    """Per-kind counts + problem paths for one sync, folded into ``last-sync.json``
    and printed by ``grison status``."""

    kind: str
    counts: dict[str, int] = field(default_factory=dict)
    problem_paths: list[str] = field(default_factory=list)

    def bump(self, outcome: Outcome) -> None:
        self.counts[outcome.value] = self.counts.get(outcome.value, 0) + 1


@dataclass
class SyncResult:
    """One engine run's full result — every plan actually reached, every event
    emitted, per-kind summaries, the exit code (ENGINE.md §10), and the snapshot dir
    (when the run captured undo state)."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    snapshot_dir: str | None = None
    dry_run: bool = False

    def summary_for(self, kind: str) -> KindSummary:
        return self.summaries.setdefault(kind, KindSummary(kind=kind))

    @property
    def exit_code(self) -> int:
        for p in self.plans:
            if p.outcome in PROBLEM_OUTCOMES:
                return 1
        return 0
