"""Workspace-root ``.gitignore`` merge (task item 7): never blanket-ignores
``.grison/``, only ever adds the collision-sidecar entry, merged idempotently.
"""

from __future__ import annotations

from pathlib import Path

from grison.scaffold import gitignore as gi


def test_fresh_gitignore_has_the_sidecar_entry() -> None:
    text = gi.build_gitignore(None)
    assert "*.remote.*" in text
    assert ".grison/" not in text
    assert ".grison" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_merge_preserves_existing_lines() -> None:
    existing = "*.pyc\n__pycache__/\n"
    text = gi.build_gitignore(existing)
    assert "*.pyc" in text
    assert "__pycache__/" in text
    assert "*.remote.*" in text


def test_merge_is_idempotent() -> None:
    once = gi.build_gitignore(None)
    twice = gi.build_gitignore(once)
    assert once == twice
    assert twice.count("*.remote.*") == 1


def test_plain_run_restores_a_hand_removed_entry() -> None:
    once = gi.build_gitignore(None)
    stripped = "\n".join(
        line for line in once.splitlines()
        if line not in (gi._BEGIN, gi._END, "*.remote.*")
    ) + "\n"
    healed = gi.build_gitignore(stripped or None)
    assert "*.remote.*" in healed


def test_force_strips_and_rewrites_the_block() -> None:
    once = gi.build_gitignore(None, force=False)
    forced = gi.build_gitignore(once, force=True)
    assert forced.count("*.remote.*") == 1


def test_ensure_gitignore_writes_the_file(tmp_path: Path) -> None:
    gi.ensure_gitignore(tmp_path)
    path = tmp_path / gi.GITIGNORE_RELATIVE_PATH
    assert path.exists()
    assert "*.remote.*" in path.read_text()
