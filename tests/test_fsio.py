"""Proofs for grison/fsio.py: atomic writes survive a mid-write crash, private
files/dirs land at 0600/0700 (and existing wrong-mode ones get fixed), and no
write site anywhere else in grison/ bypasses this module."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from grison import fsio

pytestmark = pytest.mark.skipif(os.name != "posix", reason="grison is POSIX-only")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- atomicity -----------------------------------------------------------------


def test_atomic_write_text_replaces_content(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    fsio.atomic_write_text(p, "one")
    fsio.atomic_write_text(p, "two")
    assert p.read_text() == "two"


def test_atomic_write_bytes_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    fsio.atomic_write_bytes(p, b"\x00\x01\xff")
    assert p.read_bytes() == b"\x00\x01\xff"


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "c.txt"
    fsio.atomic_write_text(p, "x")
    assert p.read_text() == "x"


def test_crash_mid_write_leaves_original_content_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof the brief asks for: monkeypatch os.replace to raise mid-write and
    assert the original file survives untouched, with no temp file left behind."""
    p = tmp_path / "f.txt"
    fsio.atomic_write_text(p, "original")

    def boom(*a: object, **k: object) -> None:
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr(fsio.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        fsio.atomic_write_text(p, "new content that must never land")

    assert p.read_text() == "original"
    leftovers = list(tmp_path.iterdir())
    assert leftovers == [p], f"temp file left behind: {leftovers}"


def test_crash_mid_write_on_a_new_file_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "new.txt"

    def boom(*a: object, **k: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(fsio.os, "replace", boom)
    with pytest.raises(OSError):
        fsio.atomic_write_text(p, "x")

    assert not p.exists()
    assert list(tmp_path.iterdir()) == []


# --- privacy: files --------------------------------------------------------------


def test_private_write_text_is_0600(tmp_path: Path) -> None:
    p = tmp_path / "secret.txt"
    fsio.atomic_write_text(p, "s3cret", private=True)
    assert _mode(p) == 0o600


def test_private_write_bytes_is_0600(tmp_path: Path) -> None:
    p = tmp_path / "secret.bin"
    fsio.atomic_write_bytes(p, b"s3cret", private=True)
    assert _mode(p) == 0o600


def test_non_private_write_is_not_0600(tmp_path: Path) -> None:
    """Workspace content (tracked findings/pages/etc.) is not secret — it should
    not silently become unreadable to the rest of the user's tooling."""
    p = tmp_path / "finding.md"
    fsio.atomic_write_text(p, "# hi")
    assert _mode(p) != 0o600


# --- privacy: directories --------------------------------------------------------


def test_ensure_private_dir_creates_0700(tmp_path: Path) -> None:
    d = tmp_path / ".grison"
    fsio.ensure_private_dir(d)
    assert d.is_dir()
    assert _mode(d) == 0o700


def test_ensure_private_dir_creates_missing_parents_0700(tmp_path: Path) -> None:
    d = tmp_path / ".grison" / "state" / "finding"
    fsio.ensure_private_dir(d)
    assert _mode(d) == 0o700
    assert _mode(d.parent) == 0o700
    assert _mode(d.parent.parent) == 0o700


def test_ensure_private_dir_fixes_an_existing_wrong_mode(tmp_path: Path) -> None:
    d = tmp_path / ".grison"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)  # mkdir's mode= is filtered by umask; force it for the test
    assert _mode(d) == 0o755
    fsio.ensure_private_dir(d)
    assert _mode(d) == 0o700


def test_ensure_private_dir_recursive_fixes_the_whole_tree(tmp_path: Path) -> None:
    d = tmp_path / ".grison"
    sub = d / "state"
    sub.mkdir(parents=True)
    os.chmod(d, 0o755)
    os.chmod(sub, 0o755)
    f = sub / "1.json"
    f.write_text("{}")
    os.chmod(f, 0o644)

    fsio.ensure_private_dir(d, recursive=True)

    assert _mode(d) == 0o700
    assert _mode(sub) == 0o700
    assert _mode(f) == 0o600


# --- open_private ------------------------------------------------------------


def test_open_private_creates_0600(tmp_path: Path) -> None:
    p = tmp_path / "lock"
    with fsio.open_private(p) as fh:
        fh.write("x")
    assert _mode(p) == 0o600
    assert p.read_text() == "x"


def test_open_private_fixes_existing_wrong_mode(tmp_path: Path) -> None:
    p = tmp_path / "lock"
    p.write_text("old")
    os.chmod(p, 0o644)
    with fsio.open_private(p) as fh:
        fh.write("new")
    assert _mode(p) == 0o600
    assert p.read_text() == "new"


def test_open_private_keeps_the_same_inode_across_calls(tmp_path: Path) -> None:
    """The whole point vs. an atomic replace: the file identity a concurrent
    ``flock`` holder sees must not change out from under it."""
    p = tmp_path / "lock"
    with fsio.open_private(p) as fh:
        fh.write("a")
    inode_before = p.stat().st_ino
    with fsio.open_private(p) as fh:
        fh.write("b")
    assert p.stat().st_ino == inode_before


# --- no bypasses -------------------------------------------------------------


def test_no_direct_file_writes_outside_fsio() -> None:
    """Every plain ``.write_text(``/``.write_bytes(``/``open(..., "w")`` write site
    in grison/ must go through this module instead — the audit's proof that the
    duplication it found is actually gone, not just given an alternative."""
    root = Path(__file__).resolve().parent.parent / "grison"
    pattern = re.compile(r"\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w[\"']")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "fsio.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root.parent)}:{lineno}: {line.strip()}")
    assert offenders == [], "direct write site(s) bypassing grison/fsio.py:\n" + "\n".join(
        offenders
    )
