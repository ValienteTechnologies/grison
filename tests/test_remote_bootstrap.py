"""Phase-7 tests: credential loading and first-run workspace bootstrap."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from grison import manifest as manifest_mod
from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.creds import Creds, MissingCreds, load

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
    # gw_token is one of the four secret fields (item 7, fix-fin1) — SecretStr, not
    # str, so the raw value is only reachable via get_secret_value().
    assert creds.gw_url == "https://gw.example" and creds.gw_token.get_secret_value() == "tok"
    assert creds.cf_headers() == {"CF-Access-Client-Id": "cid", "CF-Access-Client-Secret": "sec"}
    creds.require_ghostwriter()  # complete → no raise


def test_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".grison").mkdir()
    (tmp_path / ".grison" / "env").write_text("GRISON_GW_TOKEN=from-file\n")
    monkeypatch.setenv("GRISON_GW_TOKEN", "from-env")
    assert load(tmp_path).gw_token.get_secret_value() == "from-env"


def test_creds_repr_and_str_never_contain_a_secret_value() -> None:
    """Item 7 (LOW, fix-fin1): the four secret fields (``gw_token``,
    ``bs_token_id``, ``bs_token_secret``, ``cf_client_secret``) are ``SecretStr``,
    not plain ``str`` — a stray debug log, an uncaught-exception traceback frame,
    or a test-failure printing the whole ``Creds`` object must never leak a real
    token/secret. ``gw_url``/``bs_url``/``cf_client_id`` are not secrets and stay
    plain ``str``, so they DO appear verbatim."""
    creds = Creds(
        gw_url="https://gw.example",
        gw_token="super-secret-gw-token",
        bs_url="https://bs.example",
        bs_token_id="super-secret-bs-id",
        bs_token_secret="super-secret-bs-secret",
        cf_client_id="cf-client-id",
        cf_client_secret="super-secret-cf-secret",
    )
    for secret in (
        "super-secret-gw-token",
        "super-secret-bs-id",
        "super-secret-bs-secret",
        "super-secret-cf-secret",
    ):
        assert secret not in repr(creds)
        assert secret not in str(creds)
    # the non-secret fields are unaffected — still readable in repr/str.
    assert "https://gw.example" in repr(creds)
    assert "cf-client-id" in repr(creds)
    # the real values are still reachable through get_secret_value() — this is
    # about accidental logging, not about grison itself losing access to creds.
    assert creds.gw_token.get_secret_value() == "super-secret-gw-token"
    assert creds.bs_token_id.get_secret_value() == "super-secret-bs-id"
    assert creds.bs_token_secret.get_secret_value() == "super-secret-bs-secret"
    assert creds.cf_client_secret.get_secret_value() == "super-secret-cf-secret"


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
    # tests changed on purpose (workspace-format v2, BRIEF "Workspace format v2 —
    # layout and ownership": ".gitignore must NOT ignore `.grison/` wholesale (the v1
    # scaffold did; the migration rewrites that line)") — a FRESH bootstrap now writes
    # format v2 straight away: .grison/.gitignore is the private-file allow-list,
    # manifest.yml/index.json (both tracked) exist from the first run, and (task
    # "self-contained workspace", grison/scaffold/) the workspace root's OWN
    # .gitignore is now also scaffolded (idempotently merged), but only ever gets the
    # collision-sidecar entry — it must still never blanket-ignore .grison/.
    result = bootstrap_workspace(tmp_path)
    assert (tmp_path / "findings" / "library").is_dir()
    assert (tmp_path / "methodology" / "checklists").is_dir()
    env = tmp_path / ".grison" / "env"
    assert env.exists() and result.env_created
    assert stat.S_IMODE(env.stat().st_mode) == 0o600  # creds are secret
    root_gitignore = (tmp_path / ".gitignore").read_text()
    assert "*.remote.*" in root_gitignore
    assert ".grison/" not in root_gitignore  # never blanket-ignored
    grison_gitignore = (tmp_path / ".grison" / ".gitignore").read_text()
    assert "!manifest.yml" in grison_gitignore and "!index.json" in grison_gitignore
    assert manifest_mod.read(tmp_path).format == manifest_mod.CURRENT_FORMAT
    assert (tmp_path / ".grison" / "index.json").exists()


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / ".grison" / "env").write_text("GRISON_GW_TOKEN=filled\n")  # user filled it
    (tmp_path / ".grison" / "index.json").write_text('{"version": 1, "records": {}}\n')
    second = bootstrap_workspace(tmp_path)
    assert not second.env_created  # template not overwritten
    assert (tmp_path / ".grison" / "env").read_text() == "GRISON_GW_TOKEN=filled\n"
    # manifest/index from the first run are not clobbered by a second bootstrap
    assert manifest_mod.read(tmp_path).format == manifest_mod.CURRENT_FORMAT
    assert (tmp_path / ".grison" / "index.json").read_text() == '{"version": 1, "records": {}}\n'


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
