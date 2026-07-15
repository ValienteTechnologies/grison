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
