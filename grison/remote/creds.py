"""Credentials — read from the workspace ``.grison/env``, overridable by env vars.

The binary scaffolds a commented template (see :mod:`grison.remote.bootstrap`); the
human only pastes values. ``GRISON_*`` environment variables override the file so
headless/CI runs need no file. If the deployment sits behind Cloudflare Access ZTNA,
set the CF service-token pair and it is sent with every remote call; leave both empty
otherwise.

The same file also carries a couple of non-secret behavioral settings (:class:`Settings`)
— same file/env precedence, kept separate from :class:`Creds` because they're not secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# field name -> env var / .grison/env key
_KEYS: dict[str, str] = {
    "gw_url": "GRISON_GW_URL",
    "gw_token": "GRISON_GW_TOKEN",
    "bs_url": "GRISON_BS_URL",
    "bs_token_id": "GRISON_BS_TOKEN_ID",
    "bs_token_secret": "GRISON_BS_TOKEN_SECRET",
    "cf_client_id": "GRISON_CF_CLIENT_ID",
    "cf_client_secret": "GRISON_CF_CLIENT_SECRET",
}

# field name -> env var / .grison/env key, for the non-secret behavioral settings
_SETTING_KEYS: dict[str, str] = {
    "git": "GRISON_GIT",
    "claude_md": "GRISON_CLAUDE_MD",
}


class MissingCreds(RuntimeError):
    """Required credentials are absent — the message tells the user what to fill."""


@dataclass(frozen=True)
class Creds:
    gw_url: str = ""
    gw_token: str = ""
    bs_url: str = ""
    bs_token_id: str = ""
    bs_token_secret: str = ""
    cf_client_id: str = ""
    cf_client_secret: str = ""

    def cf_headers(self) -> dict[str, str]:
        if not (self.cf_client_id and self.cf_client_secret):
            return {}
        return {
            "CF-Access-Client-Id": self.cf_client_id,
            "CF-Access-Client-Secret": self.cf_client_secret,
        }

    def require_ghostwriter(self) -> None:
        missing = [_KEYS[k] for k in ("gw_url", "gw_token") if not getattr(self, k)]
        if missing:
            raise MissingCreds(
                "missing Ghostwriter credentials: "
                + ", ".join(missing)
                + "\nFill them into .grison/env (or set the env vars) and re-run."
            )
        self._require_cf_pair()

    def require_bookstack(self) -> None:
        needed = ("bs_url", "bs_token_id", "bs_token_secret")
        missing = [_KEYS[k] for k in needed if not getattr(self, k)]
        if missing:
            raise MissingCreds(
                "missing BookStack credentials: "
                + ", ".join(missing)
                + "\nFill them into .grison/env (or set the env vars) and re-run."
            )
        self._require_cf_pair()

    def _require_cf_pair(self) -> None:
        if bool(self.cf_client_id) != bool(self.cf_client_secret):
            raise MissingCreds(
                "GRISON_CF_CLIENT_ID and GRISON_CF_CLIENT_SECRET must be set together "
                "(Cloudflare Access service token) — set both, or neither if the "
                "deployment is not behind CF Access."
            )


@dataclass(frozen=True)
class Settings:
    """Non-secret behavioral toggles, opt-in and off by default (except ``claude_md``,
    which is opt-out) so a bare workspace behaves exactly as before these existed."""

    git: str = ""  # "" (default, off) | "commit" — let grison drive git around sync/parse
    claude_md: str = ""  # "" (default, on) | "off" — scaffold CLAUDE.md on first bootstrap

    @property
    def git_commit(self) -> bool:
        return self.git == "commit"

    @property
    def claude_md_enabled(self) -> bool:
        return self.claude_md != "off"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _resolve(keys: dict[str, str], file_vals: dict[str, str]) -> dict[str, str]:
    return {
        field: os.environ.get(env_key) or file_vals.get(env_key, "") or ""
        for field, env_key in keys.items()
    }


def load(root: Path) -> Creds:
    """Load creds for a workspace: ``.grison/env`` values, overlaid by ``GRISON_*`` env vars."""
    file_vals = _parse_env_file(root / ".grison" / "env")
    return Creds(**_resolve(_KEYS, file_vals))


def load_settings(root: Path) -> Settings:
    """Load behavioral settings for a workspace — same file/env precedence as :func:`load`."""
    file_vals = _parse_env_file(root / ".grison" / "env")
    return Settings(**_resolve(_SETTING_KEYS, file_vals))
