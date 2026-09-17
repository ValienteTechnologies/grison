"""Shared helpers for the workspace-format-v2 validator tests. Not itself a test
module (no ``test_`` prefix, so pytest never collects it and
``test_spec_coverage.py`` never scans it for rule markers)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from grison.validator.registry import Failure

FIXTURE = Path(__file__).parent / "fixtures" / "ws-v2"


def copy_fixture(tmp_path: Path, *, git: bool = False) -> Path:
    """A fresh, independent copy of the fixture workspace under ``tmp_path`` — every
    failing-case test copies this and breaks exactly one thing, so no test can ever
    contaminate another. ``git=True`` additionally ``git init``s it (bare, no commit —
    ``git check-ignore`` works against the working tree alone), needed for the WS-008
    git-hygiene tests, since pytest's ``tmp_path`` is not itself inside a repo."""
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE, dest)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    return dest


def rule_ids(fails: list[Failure]) -> set[str]:
    return {f.rule_id for f in fails}


def edit(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``path`` — ``old`` must appear at least once
    (a typo'd fixture edit fails loudly instead of silently doing nothing)."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
