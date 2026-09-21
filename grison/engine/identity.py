"""Index-backed identity: the M/U move-pairing pass (D3, D4; ENGINE.md 'Identity').

Every sync resolves, per record kind, three sets: indexed-and-present (an ordinary
record — classify.py handles it directly), indexed-but-missing ("M" — the file that
used to be at this path is gone), and present-but-unindexed ("U" — a file sync has
never seen before). :func:`pair` matches M against U: identical content to the
record's last-synced base -> MOVE; similar content (>= 0.6 normalised-text similarity)
against the REMOTE record's current content -> MOVE + EDIT; otherwise unpaired.

Pairing is one-to-one, best score first, ties broken deterministically by path, and
never crosses record kinds (an M of kind "bs.page" only ever pairs against a U of kind
"bs.page") — the caller (:mod:`grison.engine.documents`) is what actually enforces the
"any directory" part of "moving a page to another chapter is a move": it hands this
module every M/U of one kind across the WHOLE scope being synced, not per-directory.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import PurePosixPath


def normalized_similarity(a: str, b: str) -> float:
    """Whitespace-collapsed :class:`difflib.SequenceMatcher` ratio — cheap, stdlib-only,
    deterministic, and insensitive to reformatting-only differences (which would
    otherwise make an untouched-but-reflowed move look like an edit)."""
    na = " ".join(a.split())
    nb = " ".join(b.split())
    if not na and not nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


@dataclass(frozen=True)
class Missing:
    """One indexed-but-missing record ("M")."""

    path: PurePosixPath
    id: int
    base_hash: str | None  # merge base hash, for the "identical" check
    remote_content: str | None  # the REMOTE record's current canonical text, for "similar"


@dataclass(frozen=True)
class Unindexed:
    """One present-but-unindexed file ("U")."""

    path: PurePosixPath
    content: str  # canonical text, hashed the same way base_hash was computed
    content_hash: str


@dataclass(frozen=True)
class PairDecision:
    """One resolved M/U pair."""

    old_path: PurePosixPath
    new_path: PurePosixPath
    id: int
    edited: bool  # False = MOVE (identical content); True = MOVE + EDIT


@dataclass
class PairingResult:
    decisions: list[PairDecision]
    unpaired_missing: list[Missing]
    unpaired_unindexed: list[Unindexed]


_SIMILARITY_THRESHOLD = 0.6


def pair(missing: list[Missing], unindexed: list[Unindexed]) -> PairingResult:
    """Resolve every M against every U of the SAME kind (callers only ever pass one
    kind's lists in). Deterministic: candidates are scored, then taken in
    (score desc, m.path asc, u.path asc) order, greedily, each M and U usable once."""
    candidates: list[tuple[float, bool, PurePosixPath, PurePosixPath]] = []
    for m in missing:
        for u in unindexed:
            identical = m.base_hash is not None and u.content_hash == m.base_hash
            if identical:
                candidates.append((1.0, False, m.path, u.path))
                continue
            if m.remote_content is None:
                continue
            score = normalized_similarity(u.content, m.remote_content)
            if score >= _SIMILARITY_THRESHOLD:
                candidates.append((score, True, m.path, u.path))
    # highest score first; ties broken by path (m.path then u.path) ascending
    candidates.sort(key=lambda c: (-c[0], str(c[2]), str(c[3])))

    by_path_m = {m.path: m for m in missing}
    used_m: set[PurePosixPath] = set()
    used_u: set[PurePosixPath] = set()
    decisions: list[PairDecision] = []
    for _score, edited, mp, up in candidates:
        if mp in used_m or up in used_u:
            continue
        used_m.add(mp)
        used_u.add(up)
        decisions.append(PairDecision(old_path=mp, new_path=up, id=by_path_m[mp].id, edited=edited))

    unpaired_missing = [m for m in missing if m.path not in used_m]
    unpaired_unindexed = [u for u in unindexed if u.path not in used_u]
    return PairingResult(decisions, unpaired_missing, unpaired_unindexed)
