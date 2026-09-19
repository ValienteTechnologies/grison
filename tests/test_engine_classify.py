"""One test per row of ENGINE.md's classification table, plus force-flag variants and
read-only/append-only modes. Pure function — no fixtures, no I/O."""

from __future__ import annotations

import pytest

from grison.engine.classify import classify
from grison.engine.model import Outcome

H1, H2, BASE = "sha256:aaa", "sha256:bbb", "sha256:ccc"


# Row 1a: present+indexed, remote present, L == R, base == L -> CLEAN
def test_clean() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=H1,
        )
        is Outcome.CLEAN
    )


# Row 1b: present+indexed, remote present, L == R, base != L -> REPAIR (restamp only)
def test_repair_when_base_disagrees_but_sides_match() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=BASE,
        )
        is Outcome.REPAIR
    )


# Row 1c: present+indexed, remote present, L == R, base is None -> CLEAN (no base to repair)
def test_clean_when_sides_match_and_no_base() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=None,
        )
        is Outcome.CLEAN
    )


# Row 2: present+indexed, remote present, base set, L != base, R == base -> PUSH
def test_push() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=BASE,
            base_hash=BASE,
        )
        is Outcome.PUSH
    )


# Row 3: present+indexed, remote present, base set, L == base, R != base -> PULL
def test_pull() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=BASE,
            remote_hash=H2,
            base_hash=BASE,
        )
        is Outcome.PULL
    )


# Row 4: present+indexed, remote present, L != R, both != base (base set) -> COLLISION
def test_collision_both_diverged_from_base() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=BASE,
        )
        is Outcome.COLLISION
    )


# Row 4 variant: L != R, base is None (no state at all) -> COLLISION
def test_collision_no_base_and_sides_differ() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=None,
        )
        is Outcome.COLLISION
    )


# Row 5: present, unindexed -> CREATE
def test_create() -> None:
    assert (
        classify(
            indexed=False,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=None,
        )
        is Outcome.CREATE
    )


# Row 6: missing, indexed, remote present, base set, R == base -> DELETE_REMOTE
def test_delete_remote() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=BASE,
            base_hash=BASE,
        )
        is Outcome.DELETE_REMOTE
    )


# Row 7: missing, indexed, remote present, base set, R != base -> COLLISION
def test_collision_deleted_locally_edited_remotely() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=H2,
            base_hash=BASE,
        )
        is Outcome.COLLISION
    )


# Row 8: present, indexed, remote gone, base set, L == base -> DELETE_LOCAL
def test_delete_local() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=BASE,
            remote_hash=None,
            base_hash=BASE,
        )
        is Outcome.DELETE_LOCAL
    )


# Row 9: present, indexed, remote gone, base set, L != base -> COLLISION
def test_collision_edited_locally_deleted_remotely() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=BASE,
        )
        is Outcome.COLLISION
    )


# Row 9 variant: remote gone, no base at all -> conservative COLLISION (never a guessed
# delete — see classify.py's docstring on this extension of the table)
def test_collision_remote_gone_no_base() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=None,
        )
        is Outcome.COLLISION
    )


# Row 10: missing, indexed, remote gone, any base -> FORGET
def test_forget() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=False,
            local_hash=None,
            remote_hash=None,
            base_hash=BASE,
        )
        is Outcome.FORGET
    )
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=False,
            local_hash=None,
            remote_hash=None,
            base_hash=None,
        )
        is Outcome.FORGET
    )


# Row 11: absent locally, remote present, unindexed -> PULL_NEW
def test_pull_new() -> None:
    assert (
        classify(
            indexed=False,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=H1,
            base_hash=None,
        )
        is Outcome.PULL_NEW
    )


def test_raises_for_nothing_to_classify() -> None:
    with pytest.raises(ValueError, match="nothing to classify"):
        classify(
            indexed=False,
            local_present=False,
            remote_present=False,
            local_hash=None,
            remote_hash=None,
            base_hash=None,
        )


# --- force-local / force-remote (resolve a collision) -------------------------------


def test_force_local_on_ordinary_collision_pushes() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=BASE,
            force_local=True,
        )
        is Outcome.PUSH
    )


def test_force_remote_on_ordinary_collision_pulls() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=BASE,
            force_remote=True,
        )
        is Outcome.PULL
    )


def test_force_local_on_deleted_locally_edited_remotely_deletes_remote() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=H2,
            base_hash=BASE,
            force_local=True,
        )
        is Outcome.DELETE_REMOTE
    )


def test_force_remote_on_deleted_locally_edited_remotely_pulls() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=H2,
            base_hash=BASE,
            force_remote=True,
        )
        is Outcome.PULL
    )


def test_force_remote_on_edited_locally_deleted_remotely_deletes_local() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=BASE,
            force_remote=True,
        )
        is Outcome.DELETE_LOCAL
    )


def test_force_local_on_edited_locally_deleted_remotely_recreates_remote() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=BASE,
            force_local=True,
        )
        is Outcome.PUSH
    )


def test_force_flags_have_no_effect_outside_a_collision() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=H1,
            force_local=True,
            force_remote=True,
        )
        is Outcome.CLEAN
    )


# --- read-only record types ----------------------------------------------------------


def test_read_only_never_pushes() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=BASE,
            base_hash=BASE,
            read_only=True,
        )
        is Outcome.INVALID
    )


def test_read_only_never_creates() -> None:
    assert (
        classify(
            indexed=False,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=None,
            read_only=True,
        )
        is Outcome.INVALID
    )


def test_read_only_never_deletes_remote() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=BASE,
            base_hash=BASE,
            read_only=True,
        )
        is Outcome.INVALID
    )


def test_read_only_collision_is_invalid_not_surfaced_as_collision() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=BASE,
            read_only=True,
        )
        is Outcome.INVALID
    )


def test_read_only_still_pulls() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=BASE,
            remote_hash=H2,
            base_hash=BASE,
            read_only=True,
        )
        is Outcome.PULL
    )


def test_read_only_still_pulls_new() -> None:
    assert (
        classify(
            indexed=False,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=H1,
            base_hash=None,
            read_only=True,
        )
        is Outcome.PULL_NEW
    )


def test_read_only_still_deletes_local() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=BASE,
            remote_hash=None,
            base_hash=BASE,
            read_only=True,
        )
        is Outcome.DELETE_LOCAL
    )


def test_read_only_still_cleans() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=H1,
            read_only=True,
        )
        is Outcome.CLEAN
    )


# --- append-only record types ---------------------------------------------------------


def test_append_only_creates_from_new_local_file() -> None:
    assert (
        classify(
            indexed=False,
            local_present=True,
            remote_present=False,
            local_hash=H1,
            remote_hash=None,
            base_hash=None,
            append_only=True,
        )
        is Outcome.CREATE
    )


def test_append_only_indexed_record_stays_clean_when_unchanged() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=H1,
            append_only=True,
        )
        is Outcome.CLEAN
    )


def test_append_only_indexed_record_pulls_a_remote_edit() -> None:
    """An already-indexed append-only record classifies exactly like a read-only
    one — ENGINE.md: "Read-only record types... only ever take PULL / PULL_NEW /
    DELETE_LOCAL"; a remote edit to an existing mirrored note DOES pull, it is only
    ever PUSHed/DELETE_REMOTEd that append-only forbids."""
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=BASE,
            remote_hash=H2,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.PULL
    )


def test_append_only_indexed_record_still_deletes_local() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=False,
            local_hash=BASE,
            remote_hash=None,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.DELETE_LOCAL
    )


def test_append_only_indexed_record_still_repairs() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H1,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.REPAIR
    )


def test_append_only_indexed_record_still_forgets() -> None:
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=False,
            local_hash=None,
            remote_hash=None,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.FORGET
    )


def test_append_only_never_pushes_a_local_edit() -> None:
    """A local edit to an already-indexed append-only record (mirrored note) is
    never a PUSH — it clamps to INVALID (the same defense-in-depth read-only
    relies on; the real enforcement is upstream, in the validator)."""
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=BASE,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.INVALID
    )


def test_append_only_never_deletes_remote() -> None:
    """A local file deleted, remote unmodified since the last sync, would
    otherwise be DELETE_REMOTE for a read-write kind — append-only clamps this to
    INVALID instead (it never deletes the remote record)."""
    assert (
        classify(
            indexed=True,
            local_present=False,
            remote_present=True,
            local_hash=None,
            remote_hash=BASE,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.INVALID
    )


def test_append_only_collision_clamps_to_invalid() -> None:
    assert (
        classify(
            indexed=True,
            local_present=True,
            remote_present=True,
            local_hash=H1,
            remote_hash=H2,
            base_hash=BASE,
            append_only=True,
        )
        is Outcome.INVALID
    )
