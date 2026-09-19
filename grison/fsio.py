"""File-system primitives shared by every write path in grison.

Every file grison writes is either (a) tracked workspace content the user reads and
edits, or (b) private housekeeping under ``.grison/`` (creds, state, lock, mirrors,
snapshots). Both share the same two hazards: a crash mid-write must never leave a
half-written file behind, and a private file must never be briefly world/group
readable on its way to existing. :func:`atomic_write_text`/:func:`atomic_write_bytes`
close the first hazard (temp file in the same directory, fsync, then ``os.replace`` —
atomic on POSIX, and a crash or exception before the replace leaves the previous
content, or no file at all, exactly as it was). ``private=True`` closes the second
(the temp file is created 0600 from the start, never 0644-then-chmod, which would
leak the content at a wider mode for however long the chmod takes to run).

:func:`ensure_private_dir` is the directory counterpart (0700), and — unlike the
write functions, which only ever create new files — it also *fixes* the mode of a
directory that already exists with a wider one, since ``.grison/`` is created once
and nothing else in the codebase re-checks its permissions afterwards.

:func:`open_private` is for the one case that can't go through atomic replace: a
file whose identity (inode) must stay stable across the call, namely the workspace
lock file held open under ``flock`` for a run's duration. An atomic replace would
swap the inode out from under a concurrent opener and defeat the lock.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO

_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600
_PRIVATE_DIR_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0700
_DEFAULT_FILE_MODE = 0o644


def _atomic_write(path: Path, data: bytes, *, private: bool) -> None:
    if private:
        # A private file's parent directory must be 0700 from the moment it is
        # created — a bare `mkdir` is umask-governed (a lenient umask, e.g. 0o002,
        # would leave a brand-new `.grison/state/<kind>/` world/group-readable for
        # however long until something else happened to tighten it), so this reuses
        # the same private-dir helper `.grison/` itself is bootstrapped with, which
        # creates every missing ancestor 0700 and belt-and-suspenders chmods each
        # one regardless of umask (item 4, fix-fin1).
        ensure_private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(data)}")
    mode = _PRIVATE_FILE_MODE if private else _DEFAULT_FILE_MODE
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX — no half-written file, ever
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes, *, private: bool = False) -> None:
    """Write ``data`` to ``path`` atomically (temp file in the same directory,
    fsync, ``os.replace``). ``private=True`` creates the file 0600 from the start."""
    _atomic_write(path, data, private=private)


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Text sibling of :func:`atomic_write_bytes` (UTF-8)."""
    _atomic_write(path, text.encode("utf-8"), private=private)


def ensure_private_dir(path: Path, *, recursive: bool = False) -> None:
    """Create ``path`` 0700 if missing, or fix its mode to 0700 if it already
    exists with a wider one. Every directory created along the way (when ``path``'s
    parents don't exist yet) is also made 0700 — ``Path.mkdir(mode=...)`` alone isn't
    enough since the process umask can only narrow the requested mode, and a lenient
    umask would leave an ancestor directory at a wider one.

    ``recursive=True`` additionally walks the existing tree, fixing every file to
    0600 and every subdirectory to 0700 — for self-healing a whole private tree
    (e.g. a workspace's ``.grison/`` copied or extracted with different perms) in
    one call.
    """
    if path.exists():
        os.chmod(path, _PRIVATE_DIR_MODE)
    else:
        to_create: list[Path] = []
        cur = path
        while not cur.exists():
            to_create.append(cur)
            cur = cur.parent
        for d in reversed(to_create):
            d.mkdir(mode=_PRIVATE_DIR_MODE)
            os.chmod(d, _PRIVATE_DIR_MODE)  # belt & suspenders against umask

    if recursive:
        for dirpath, dirnames, filenames in os.walk(path):
            for name in dirnames:
                os.chmod(Path(dirpath) / name, _PRIVATE_DIR_MODE)
            for name in filenames:
                os.chmod(Path(dirpath) / name, _PRIVATE_FILE_MODE)


def open_private(path: Path, mode: str = "w") -> IO[str]:
    """Open ``path`` in place — never an atomic replace — as a private (0600) text
    file, creating it if missing and fixing its mode if it already exists with a
    wider one. For a handle whose inode must stay stable across the call (the
    workspace lock file, held open under ``flock`` for a run's duration): an atomic
    replace would swap the file out from under a concurrent opener and defeat the
    lock, so this opens (and truncates, for a ``"w"``-family mode) the existing
    inode directly instead.
    """
    existed = path.exists()
    flags = os.O_RDWR | os.O_CREAT
    if "w" in mode:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    if existed:
        os.fchmod(fd, _PRIVATE_FILE_MODE)  # fix a pre-existing file's mode too
    return os.fdopen(fd, mode, encoding="utf-8")
