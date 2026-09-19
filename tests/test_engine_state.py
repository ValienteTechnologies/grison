"""``grison.engine.state.StateStore`` — private per-record JSON under
``.grison/state/<kind>/<id>.json`` (item 4, fix-fin1: this used to rely on a dead
``ensure_dirs`` no caller ever invoked, while the real write path,
``grison.fsio._atomic_write``, created parent directories with a bare, umask-
governed ``mkdir``)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from grison.engine.state import StateStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="grison is POSIX-only")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_first_put_for_a_new_kind_creates_0700_dirs_under_a_lenient_umask(
    tmp_path: Path,
) -> None:
    """Item 4 (HIGH, fix-fin1): a workspace whose ``.grison/state/`` never held this
    kind before (a brand-new record type, or a fresh workspace) must get 0700
    directories for its very first ``StateStore.put`` — even under a lenient
    process umask (e.g. 0o002, which a bare ``mkdir`` would otherwise narrow the
    request to 0775, not 0700)."""
    old_umask = os.umask(0o002)
    try:
        state = StateStore(tmp_path)
        state.put("gw.finding", 1, base="abc123", witness={})
    finally:
        os.umask(old_umask)

    kind_dir = tmp_path / ".grison" / "state" / "gw.finding"
    assert kind_dir.is_dir()
    assert _mode(kind_dir) == 0o700
    assert _mode(kind_dir.parent) == 0o700  # .grison/state/
    assert _mode(kind_dir / "1.json") == 0o600


def test_state_store_has_no_ensure_dirs_method() -> None:
    """``ensure_dirs`` was dead code — no caller anywhere ever invoked it (the real
    directory-creation guarantee is ``grison.fsio._atomic_write``'s own, exercised
    by every ``put``/``save_mirrors``/``save_last_sync`` call) — removed rather than
    kept as an unused, untested surface."""
    assert not hasattr(StateStore, "ensure_dirs")
