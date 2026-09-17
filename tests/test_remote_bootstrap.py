"""Phase-7 tests: credential loading and first-run workspace bootstrap."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.creds import MissingCreds, load

_GW_VARS = ("GRISON_GW_URL", "GRISON_GW_TOKEN", "GRISON_CF_CLIENT_ID", "GRISON_CF_CLIENT_SECRET")


def test_load_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GW_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text(
        "# comment\nGRISON_GW_URL=https://gw.example\nGRISON_GW_TOKEN=tok\n"
        "GRISON_CF_CLIENT_ID=cid\nGRISON_CF_CLIENT_SECRET=sec\n"
    )
    creds = load(tmp_path)
    assert creds.gw_url == "https://gw.example" and creds.gw_token == "tok"
    assert creds.cf_headers() == {"CF-Access-Client-Id": "cid", "CF-Access-Client-Secret": "sec"}
    creds.require_ghostwriter()  # complete → no raise


def test_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("GRISON_GW_TOKEN=from-file\n")
    monkeypatch.setenv("GRISON_GW_TOKEN", "from-env")
    assert load(tmp_path).gw_token == "from-env"


def test_require_ghostwriter_lists_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GW_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCreds, match="GRISON_GW_TOKEN"):
        load(tmp_path).require_ghostwriter()


def test_cf_access_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GW_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text(
        "GRISON_GW_URL=https://gw.example\nGRISON_GW_TOKEN=tok\n"
    )
    creds = load(tmp_path)
    creds.require_ghostwriter()  # no CF pair → still complete
    assert creds.cf_headers() == {}


def test_half_set_cf_pair_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GW_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text(
        "GRISON_GW_URL=https://gw.example\nGRISON_GW_TOKEN=tok\nGRISON_CF_CLIENT_ID=cid\n"
    )
    with pytest.raises(MissingCreds, match="set together"):
        load(tmp_path).require_ghostwriter()


def test_bootstrap_scaffolds_tree_env_and_gitignore(tmp_path: Path) -> None:
    result = bootstrap_workspace(tmp_path)
    assert (tmp_path / "findings" / "library").is_dir()
    assert (tmp_path / "methodology" / "checklists").is_dir()
    env = tmp_path / ".grison" / "env"
    assert env.exists() and result.env_created
    assert stat.S_IMODE(env.stat().st_mode) == 0o600  # creds are secret
    assert ".grison/" in (tmp_path / ".gitignore").read_text()


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / ".grison" / "env").write_text("GRISON_GW_TOKEN=filled\n")  # user filled it
    second = bootstrap_workspace(tmp_path)
    assert not second.env_created  # template not overwritten
    assert (tmp_path / ".grison" / "env").read_text() == "GRISON_GW_TOKEN=filled\n"
    # gitignore entry not duplicated
    assert (tmp_path / ".gitignore").read_text().count(".grison/") == 1


def test_bootstrap_scaffolds_claude_md_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRISON_CLAUDE_MD", raising=False)
    result = bootstrap_workspace(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists() and result.claude_md_created
    assert "grison workspace" in claude_md.read_text()


def test_bootstrap_never_overwrites_existing_claude_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRISON_CLAUDE_MD", raising=False)
    (tmp_path / "CLAUDE.md").write_text("# my own notes\n")
    result = bootstrap_workspace(tmp_path)
    assert not result.claude_md_created
    assert (tmp_path / "CLAUDE.md").read_text() == "# my own notes\n"


def test_bootstrap_skips_claude_md_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRISON_CLAUDE_MD", "off")
    result = bootstrap_workspace(tmp_path)
    assert not result.claude_md_created
    assert not (tmp_path / "CLAUDE.md").exists()


def test_bootstrap_tightens_existing_grison_permissions(tmp_path: Path) -> None:
    """A hand-created (or copied-in) .grison/ with wide permissions must be tightened
    on every bootstrap run, not just the first time the directory is created — proves
    the fix: recursive=True walks and fixes files/dirs that already existed."""
    grison_dir = tmp_path / ".grison"
    grison_dir.mkdir(mode=0o755)
    env = grison_dir / "env"
    env.write_text("GRISON_GW_TOKEN=preexisting\n")
    env.chmod(0o664)  # group/other-readable — must not survive bootstrap
    state_dir = grison_dir / "state"
    state_dir.mkdir(mode=0o755)

    bootstrap_workspace(tmp_path)

    assert stat.S_IMODE(grison_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    # content untouched — only permissions changed
    assert env.read_text() == "GRISON_GW_TOKEN=preexisting\n"
