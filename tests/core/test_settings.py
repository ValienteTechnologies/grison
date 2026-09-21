"""Settings — non-secret behavioral toggles read from .grison/env, same file/env
precedence as Creds (see grison.remote.creds)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.remote.creds import Settings, load_settings

_SETTING_VARS = ("GRISON_GIT", "GRISON_CLAUDE_MD")


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SETTING_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_are_git_off_and_claude_md_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear(monkeypatch)
    s = load_settings(tmp_path)  # no .grison/env at all
    assert s == Settings()
    assert s.git_commit is False
    assert s.claude_md_enabled is True


def test_loads_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("GRISON_GIT=commit\nGRISON_CLAUDE_MD=off\n")
    s = load_settings(tmp_path)
    assert s.git_commit is True
    assert s.claude_md_enabled is False


def test_unset_claude_md_in_file_stays_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("# GRISON_CLAUDE_MD=off\nGRISON_GW_URL=x\n")
    assert load_settings(tmp_path).claude_md_enabled is True


def test_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("GRISON_GIT=commit\n")
    monkeypatch.setenv("GRISON_GIT", "not-commit")  # env wins over the file value
    s = load_settings(tmp_path)
    assert s.git == "not-commit"
    assert s.git_commit is False  # only the literal "commit" enables it


def test_only_literal_commit_enables_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "true")  # not the magic value — stays off
    assert load_settings(tmp_path).git_commit is False


def test_settings_is_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    s = load_settings(tmp_path)
    with pytest.raises(AttributeError):
        s.git = "commit"  # type: ignore[misc]
