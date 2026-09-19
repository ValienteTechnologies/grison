"""The ONE classification table (ENGINE.md 'The classification table'), as one pure
function. Every input is already-computed presence flags and content hashes — this
module never touches the filesystem, the network, or state; :mod:`grison.engine.apply`
computes those inputs (via the adapter's ``canonical_local``/``canonical_remote`` and
:func:`grison.hashing.digest`) and calls :func:`classify` once per record slot.

Read-only / append-only record types (ENGINE.md 'The classification table', third
bullet) are enforced here too: a read-only record's local edit never produces PUSH/
CREATE/DELETE_REMOTE (a validation-gate failure, WS-009, is how the workspace actually
learns about the edit — this is defense in depth, not the primary mechanism); an
append-only record's ALREADY-INDEXED half behaves exactly like a read-only one (it
still PULLs/DELETE_LOCALs/REPAIRs/FORGETs normally — ENGINE.md: "Read-only record
types... only ever take PULL / PULL_NEW / DELETE_LOCAL"; append-only is that same
rule, plus the one thing read-only can't do at all: CREATE from a brand-new local
file). Append-only never PUSHes/DELETE_REMOTEs an already-indexed record either way
— a local edit to one is clamped to INVALID, the same defense-in-depth read-only
relies on.
"""

from __future__ import annotations

from grison.engine.model import Outcome


def classify(  # noqa: PLR0911, PLR0913
    *,
    indexed: bool,
    local_present: bool,
    remote_present: bool,
    local_hash: str | None,
    remote_hash: str | None,
    base_hash: str | None,
    read_only: bool = False,
    append_only: bool = False,
    force_local: bool = False,
    force_remote: bool = False,
) -> Outcome:
    """One record slot's outcome. ``indexed`` says whether this slot has an identity
    (an index entry linking a path to a remote id — D3); the caller is responsible for
    never calling this for a slot with neither a local file nor a remote record (there
    is nothing to classify).

    ``force_local``/``force_remote`` resolve a COLLISION (including its DELETE_LOCAL/
    DELETE_REMOTE flavors) the way ``--force-local PATH``/``--force-remote PATH``
    do (ENGINE.md): local wins -> push/re-create/delete-remote; remote wins ->
    pull/restore-locally/delete-local. They have no effect outside a collision —
    in particular, a PULL_NEW slot (remote-only, never indexed: there is nothing
    local to prefer and nothing to overwrite) ignores both flags by construction.
    This is a known, accepted limitation (coordinator feedback item 8c): naming a
    not-yet-pulled remote path on ``--force-local``/``--force-remote`` is simply a
    no-op for that path, not an error — it is indistinguishable from any other path
    that isn't currently a collision, and every other non-collision outcome (CLEAN,
    PUSH, PULL, CREATE, MOVE, …) is silently un-forceable the same way.
    """
    if append_only:
        outcome = _classify_append_only(
            indexed=indexed,
            local_present=local_present,
            remote_present=remote_present,
            local_hash=local_hash,
            remote_hash=remote_hash,
            base_hash=base_hash,
        )
    elif indexed:
        outcome = _classify_indexed(
            local_present=local_present,
            remote_present=remote_present,
            local_hash=local_hash,
            remote_hash=remote_hash,
            base_hash=base_hash,
        )
    elif local_present:
        outcome = Outcome.CREATE
    elif remote_present:
        outcome = Outcome.PULL_NEW
    else:
        raise ValueError(
            "classify() called for a slot with no local file, no remote "
            "record and no index entry — nothing to classify"
        )

    if read_only:
        outcome = _clamp_read_only(outcome)

    if outcome is Outcome.COLLISION:
        # BRIEF: "--force-local PATH / --force-remote PATH resolve a COLLISION for
        # that path (push / pull; for the delete variants: re-create remotely or
        # delete locally / delete remotely or restore locally)". Three collision
        # shapes reach here: both sides present (row 4, ordinary push/pull), local
        # missing + remote present (row 7, "deleted locally, edited remotely" ->
        # force-local deletes the remote too, force-remote restores it locally), and
        # local present + remote missing (row 9, "edited locally, deleted remotely"
        # -> force-local re-creates it remotely as a PUSH — apply.py's pre-write
        # re-fetch guard is what turns a PUSH with no remote record left into a
        # create — force-remote deletes the local copy to match).
        if force_local:
            if not local_present and remote_present:
                return Outcome.DELETE_REMOTE
            return Outcome.PUSH
        if force_remote:
            if local_present and not remote_present:
                return Outcome.DELETE_LOCAL
            return Outcome.PULL
    return outcome


def _classify_append_only(  # noqa: PLR0913
    *,
    indexed: bool,
    local_present: bool,
    remote_present: bool,
    local_hash: str | None,
    remote_hash: str | None,
    base_hash: str | None,
) -> Outcome:
    """Append-only (ENGINE.md): a brand-new local file CREATEs; an already-indexed
    record classifies exactly like a read-only one — it still PULLs a remote edit,
    DELETE_LOCALs when the remote copy is gone (and the local copy was never
    touched), REPAIRs a stale base, and FORGETs when both sides are gone. What it
    can never do is write anything back — a local edit to an indexed record, or a
    local delete that would otherwise DELETE_REMOTE, or a genuine COLLISION, all
    clamp to INVALID via :func:`_clamp_read_only` (never a push; the primary
    enforcement is the validator, this is defense in depth, same as read-only)."""
    if indexed:
        return _clamp_read_only(
            _classify_indexed(
                local_present=local_present,
                remote_present=remote_present,
                local_hash=local_hash,
                remote_hash=remote_hash,
                base_hash=base_hash,
            )
        )
    if local_present:
        return Outcome.CREATE
    return Outcome.CLEAN  # a remote-only append-only record with no local copy: PULL_NEW
    # is deliberately NOT returned here — callers of an append-only adapter choose
    # whether unseen remote records get pulled at all; see the note in apply.py.


def _classify_indexed(  # noqa: PLR0911
    *,
    local_present: bool,
    remote_present: bool,
    local_hash: str | None,
    remote_hash: str | None,
    base_hash: str | None,
) -> Outcome:
    if local_present and remote_present:
        if local_hash == remote_hash:
            if base_hash is not None and base_hash != local_hash:
                return Outcome.REPAIR
            return Outcome.CLEAN
        if base_hash is not None and local_hash != base_hash and remote_hash == base_hash:
            return Outcome.PUSH
        if base_hash is not None and local_hash == base_hash and remote_hash != base_hash:
            return Outcome.PULL
        return Outcome.COLLISION

    if local_present and not remote_present:
        # "deleted remotely" — DELETE_LOCAL only when we can PROVE the local copy is
        # unmodified since the last sync (base known and matches); no base at all (a
        # record whose state was never stamped, or was lost) is conservatively a
        # COLLISION rather than a guessed delete (state.py: "a record with no base
        # classifies by the table" — this is that rule's safe extension to the
        # remote-gone case, never in the table's own text but required by it not
        # silently guessing).
        if base_hash is not None and local_hash == base_hash:
            return Outcome.DELETE_LOCAL
        return Outcome.COLLISION

    if not local_present and remote_present:
        if base_hash is not None and remote_hash == base_hash:
            return Outcome.DELETE_REMOTE
        return Outcome.COLLISION

    return Outcome.FORGET  # missing, indexed, remote gone too


def _clamp_read_only(outcome: Outcome) -> Outcome:
    """A read-only record type only ever takes PULL/PULL_NEW/DELETE_LOCAL/CLEAN/
    REPAIR/FORGET. Anything the table would otherwise have written remotely, or that
    would have deleted the remote record, is impossible for a read-only adapter to act
    on (it has no create/update/delete) — surfaced as INVALID instead of silently
    dropped, since a mirror in this shape means it was hand-edited (the validator's
    WS-009 is expected to have already caught this; this is the defense-in-depth
    fallback for the case where it somehow didn't)."""
    if outcome in (Outcome.PUSH, Outcome.CREATE, Outcome.DELETE_REMOTE, Outcome.COLLISION):
        return Outcome.INVALID
    return outcome
