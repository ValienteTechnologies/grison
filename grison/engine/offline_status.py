"""One kind's status derived entirely from the index, private state, and the
validator's own failures — no adapter ``ctx``, no network at all (coordinator
feedback item 6: ``grison status`` must be able to say something real without
contacting BookStack).

Reuses exactly the same local-discovery (``adapter.scan_local``) and identity-pairing
(:mod:`grison.engine.identity`) building blocks :mod:`grison.engine.apply` uses for a
real sync, just without ``fetch_remote``/``refetch``: every indexed-but-missing local
file is paired against every present-but-unindexed one, but since there is no remote
content to compare against here, only the "identical to the last-synced base" branch
of :func:`grison.engine.identity.pair` can ever resolve — a genuine MOVE, never a
MOVE+EDIT (that needs the remote's current text, which offline never has). Every
record lands in exactly one of seven mutually-exclusive buckets:

- ``clean`` — indexed, present locally, unchanged since the last sync
- ``edited`` — indexed, present locally, changed since the last sync
- ``new`` — present locally, not indexed (not paired with a missing one either)
- ``deleted`` — indexed, not present locally (not paired with a new one either)
- ``moved`` — an indexed-missing/present-unindexed pair with identical content
- ``unknown`` — indexed and present, but there is no recorded base to compare
  against (state never captured one, or it was lost) — reported distinctly rather
  than silently guessed as clean or edited
- ``invalid`` — the validator has a failure naming this path; this OVERRIDES
  whichever of the above the record would otherwise land in, since a broken record
  is the thing the user must act on first, regardless of its edit state

A live collision sidecar (``grison.engine.apply.sidecar_path``) is reported
separately, alongside a record's own bucket, rather than as an eighth mutually
exclusive bucket — the record underneath a pending sidecar can be perfectly "clean"
by every other measure; the sidecar is an independent, additive signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from grison.engine.adapter import Adapter
from grison.engine.apply import sidecar_path
from grison.engine.identity import Missing, Unindexed, pair
from grison.engine.state import StateStore
from grison.hashing import digest
from grison.index import Index
from grison.validator.registry import Failure

BUCKETS = ("clean", "edited", "new", "deleted", "moved", "invalid", "unknown")


@dataclass(frozen=True)
class StatusEntry:
    path: PurePosixPath
    bucket: str  # one of BUCKETS
    reasons: tuple[str, ...] = ()  # rule ids; set only when bucket == "invalid"
    moved_from: PurePosixPath | None = None  # set only when bucket == "moved"


@dataclass(frozen=True)
class OfflineStatus:
    entries: list[StatusEntry]
    collision_sidecars: list[PurePosixPath]

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(BUCKETS, 0)
        for e in self.entries:
            counts[e.bucket] += 1
        return counts

    @property
    def non_clean(self) -> list[StatusEntry]:
        return [e for e in self.entries if e.bucket != "clean"]


def compute_offline_status(
    root: Path, adapter: Adapter, index: Index, state: StateStore, failures: list[Failure],
) -> OfflineStatus:
    """This kind's offline status. ``failures`` is the validator's own failure list
    (already scoped to this kind's area by the caller, e.g. ``paths=[".../methodology"]``
    — this function does no scoping of its own, it just looks up by exact path)."""
    kind = adapter.kind
    reasons_by_path: dict[str, tuple[str, ...]] = {}
    for f in failures:
        reasons_by_path[f.path] = (*reasons_by_path.get(f.path, ()), f.rule_id)

    local_docs = {d.path: d for d in adapter.scan_local(root)}
    indexed = {
        PurePosixPath(p): rec.id for p, rec in index.records.items() if rec.kind.value == kind
    }
    present_indexed = {p for p in local_docs if p in indexed}
    missing_paths = {p for p in indexed if p not in local_docs}
    unindexed_paths = {p for p in local_docs if p not in indexed}

    missing = [
        Missing(path=p, id=indexed[p], base_hash=_base_hash(state, kind, indexed[p]),
               remote_content=None)
        for p in missing_paths
    ]
    unindexed = [
        Unindexed(path=p, content=local_docs[p].raw_text,
                 content_hash=digest(adapter.canonical_local(local_docs[p].doc)))
        for p in unindexed_paths
    ]
    result = pair(missing, unindexed)
    paired_missing = {d.old_path for d in result.decisions}
    paired_unindexed = {d.new_path for d in result.decisions}

    entries: list[StatusEntry] = []
    for d in sorted(result.decisions, key=lambda d: str(d.new_path)):
        entries.append(_entry(d.new_path, "moved", reasons_by_path, moved_from=d.old_path))
    for p in sorted(present_indexed, key=str):
        rid = indexed[p]
        base_hash = _base_hash(state, kind, rid)
        local_hash = digest(adapter.canonical_local(local_docs[p].doc))
        if base_hash is None:
            bucket = "unknown"
        elif local_hash == base_hash:
            bucket = "clean"
        else:
            bucket = "edited"
        entries.append(_entry(p, bucket, reasons_by_path))
    for p in sorted(missing_paths - paired_missing, key=str):
        entries.append(_entry(p, "deleted", reasons_by_path))
    for p in sorted(unindexed_paths - paired_unindexed, key=str):
        entries.append(_entry(p, "new", reasons_by_path))

    known_paths = present_indexed | missing_paths | unindexed_paths
    sidecars = sorted(
        p for p in known_paths if (root / sidecar_path(p)).exists()
    )
    return OfflineStatus(entries=sorted(entries, key=lambda e: str(e.path)),
                         collision_sidecars=sidecars)


def _entry(
    path: PurePosixPath, bucket: str, reasons_by_path: dict[str, tuple[str, ...]], *,
    moved_from: PurePosixPath | None = None,
) -> StatusEntry:
    reasons = reasons_by_path.get(str(path))
    if reasons:
        return StatusEntry(path=path, bucket="invalid", reasons=reasons, moved_from=moved_from)
    return StatusEntry(path=path, bucket=bucket, moved_from=moved_from)


def _base_hash(state: StateStore, kind: str, rid: int) -> str | None:
    st = state.get(kind, rid)
    return st.base if st else None
