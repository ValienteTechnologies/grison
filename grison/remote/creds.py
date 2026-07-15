"""Credentials — read from the workspace ``.grison/env``, overridable by env vars.

The binary scaffolds a commented template (see :mod:`grison.remote.bootstrap`); the
human only pastes values. ``GRISON_*`` environment variables override the file so
headless/CI runs need no file. Both Ghostwriter and BookStack sit behind Cloudflare
Access ZTNA, so the CF service-token pair is required for any remote call.
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
        return {
            "CF-Access-Client-Id": self.cf_client_id,
            "CF-Access-Client-Secret": self.cf_client_secret,
        }

    def require_ghostwriter(self) -> None:
        missing = [
            _KEYS[k]
            for k in ("gw_url", "gw_token", "cf_client_id", "cf_client_secret")
            if not getattr(self, k)
        ]
        if missing:
            raise MissingCreds(
                "missing Ghostwriter credentials: "
                + ", ".join(missing)
                + "\nFill them into .grison/env (or set the env vars) and re-run."
            )

    def require_bookstack(self) -> None:
        needed = ("bs_url", "bs_token_id", "bs_token_secret", "cf_client_id", "cf_client_secret")
        missing = [_KEYS[k] for k in needed if not getattr(self, k)]
        if missing:
            raise MissingCreds(
                "missing BookStack credentials: "
                + ", ".join(missing)
                + "\nFill them into .grison/env (or set the env vars) and re-run."
            )


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


def load(root: Path) -> Creds:
    """Load creds for a workspace: ``.grison/env`` values, overlaid by ``GRISON_*`` env vars."""
    file_vals = _parse_env_file(root / ".grison" / "env")
    resolved: dict[str, str] = {}
    for field, env_key in _KEYS.items():
        resolved[field] = os.environ.get(env_key) or file_vals.get(env_key, "") or ""
    return Creds(**resolved)
