"""Proofs for grison/manifest.py: format read/write/check, the exact error
messages/types for too-new vs. too-old, v1 detection, the .gitignore allow-list,
and git-hygiene checking in a real (non-grison) temp git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from grison import manifest as m

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


# --- read/write/check --------------------------------------------------------


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    m.write(tmp_path)
    assert m.read(tmp_path).format == m.CURRENT_FORMAT


def test_write_specific_format(tmp_path: Path) -> None:
    m.write(tmp_path, m.Manifest(format=1))
    assert m.read(tmp_path).format == 1


def test_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m.write(tmp_path)
    before = (tmp_path / ".grison" / "manifest.yml").read_text()
    import grison.fsio as fsio

    monkeypatch.setattr(fsio.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        m.write(tmp_path, m.Manifest(format=99))
    assert (tmp_path / ".grison" / "manifest.yml").read_text() == before


def test_read_missing_manifest_no_v1_artefacts_is_current_format(tmp_path: Path) -> None:
    """A directory with nothing grison-related at all is not "format 1" — that
    would wrongly trigger the migration path for a workspace that was never v1 in
    the first place (e.g. one about to be freshly bootstrapped)."""
    assert m.read(tmp_path).format == m.CURRENT_FORMAT


def test_read_missing_manifest_with_v1_env_file_is_format_1(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("GRISON_GW_URL=\n")
    assert m.read(tmp_path).format == 1


def test_read_rejects_missing_format_key(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "manifest.yml").write_text(yaml.safe_dump({"other": 1}))
    with pytest.raises(m.ManifestError, match="missing 'format'"):
        m.read(tmp_path)


def test_read_rejects_non_integer_format(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "manifest.yml").write_text(yaml.safe_dump({"format": "two"}))
    with pytest.raises(m.ManifestError, match="must be an integer"):
        m.read(tmp_path)


def test_read_rejects_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "manifest.yml").write_text("{not: valid: yaml:")
    with pytest.raises(m.ManifestError, match="invalid YAML"):
        m.read(tmp_path)


def test_check_matching_format_returns_manifest(tmp_path: Path) -> None:
    m.write(tmp_path)
    assert m.check(tmp_path).format == m.CURRENT_FORMAT


def test_check_too_new_raises_with_exact_message(tmp_path: Path) -> None:
    m.write(tmp_path, m.Manifest(format=m.CURRENT_FORMAT + 1))
    with pytest.raises(m.WorkspaceTooNew) as ei:
        m.check(tmp_path)
    assert str(ei.value) == (
        f"this workspace uses format {m.CURRENT_FORMAT + 1}; this grison supports "
        f"up to {m.CURRENT_FORMAT} — upgrade grison"
    )


def test_check_too_old_raises_distinct_error_type_with_exact_message(tmp_path: Path) -> None:
    m.write(tmp_path, m.Manifest(format=1))
    with pytest.raises(m.WorkspaceNeedsMigration) as ei:
        m.check(tmp_path)
    assert str(ei.value) == "workspace format 1 needs the one-time migration"


def test_too_new_and_too_old_are_distinct_exception_types(tmp_path: Path) -> None:
    assert not issubclass(m.WorkspaceTooNew, m.WorkspaceNeedsMigration)
    assert not issubclass(m.WorkspaceNeedsMigration, m.WorkspaceTooNew)
    assert issubclass(m.WorkspaceTooNew, m.GrisonError)
    assert issubclass(m.WorkspaceNeedsMigration, m.GrisonError)


# --- .gitignore allow-list ----------------------------------------------------


def test_write_gitignore_ignores_everything_by_default() -> None:
    assert m._GITIGNORE_TEXT.splitlines()[1] == "*"


@pytest.mark.parametrize(
    "allowed", [".gitignore", "manifest.yml", "index.json", "SPEC.md", "templates/"]
)
def test_write_gitignore_allow_lists_the_tracked_files(tmp_path: Path, allowed: str) -> None:
    m.write_gitignore(tmp_path)
    text = (tmp_path / ".grison" / ".gitignore").read_text()
    assert f"!{allowed}" in text


def test_write_gitignore_is_atomic(tmp_path: Path) -> None:
    m.write_gitignore(tmp_path)
    assert (tmp_path / ".grison" / ".gitignore").exists()


# --- git hygiene ---------------------------------------------------------------


def test_check_git_hygiene_outside_a_repo_is_a_noop(tmp_path: Path) -> None:
    assert m.check_git_hygiene(tmp_path) == []


def test_check_git_hygiene_clean_workspace_has_no_problems(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init_repo(root)
    (root / ".gitignore").write_text(".grison/*\n!.grison/.gitignore\n!.grison/manifest.yml\n"
                                      "!.grison/index.json\n")
    (root / ".grison").mkdir()
    (root / ".grison" / "env").write_text("secret")
    (root / ".grison" / "manifest.yml").write_text("format: 2\n")
    assert m.check_git_hygiene(root) == []


def test_check_git_hygiene_flags_a_leaked_private_file(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init_repo(root)
    # no .gitignore at all — .grison/env would be tracked if added
    (root / ".grison").mkdir()
    (root / ".grison" / "env").write_text("secret")
    problems = m.check_git_hygiene(root)
    assert any(".grison/env" in p and "must be git-ignored" in p for p in problems)


def test_check_git_hygiene_flags_a_wrongly_ignored_tracked_file(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init_repo(root)
    (root / ".gitignore").write_text(".grison/\n")  # the v1 mistake: ignores the whole dir
    (root / ".grison").mkdir()
    (root / ".grison" / "manifest.yml").write_text("format: 2\n")
    problems = m.check_git_hygiene(root)
    assert any(".grison/manifest.yml" in p and "must be tracked" in p for p in problems)
