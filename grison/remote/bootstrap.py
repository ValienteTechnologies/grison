"""First-run scaffolding — the binary sets everything up; the human pastes creds.

Any verb in a fresh dir creates the workspace tree, ``.grison/`` (grison's private
dir, like ``.git/`` — always gitignored so creds never commit), a commented
``.grison/env`` template (chmod 600), and the ``.gitignore`` entry. There is no
``init`` and no interactive wizard: template + one message is deterministic
everywhere. ``parse`` is fully offline; ``sync`` additionally requires the creds.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from grison.workspace import bootstrap_tree

_ENV_TEMPLATE = """\
# grison credentials — paste the values, then re-run `grison sync`.
# This file lives in .grison/, which is gitignored; never commit it.
# GRISON_* environment variables override these for headless/CI use.

# Ghostwriter (Hasura GraphQL at <GW_URL>/v1/graphql; scoped bearer token)
GRISON_GW_URL=
GRISON_GW_TOKEN=

# BookStack (REST at <BS_URL>/api) — only needed for methodology sync
GRISON_BS_URL=
GRISON_BS_TOKEN_ID=
GRISON_BS_TOKEN_SECRET=

# Cloudflare Access service token — both systems sit behind CF Access ZTNA
GRISON_CF_CLIENT_ID=
GRISON_CF_CLIENT_SECRET=
"""


@dataclass
class BootstrapResult:
    created_dirs: list[Path]
    env_created: bool  # True if a fresh (unfilled) env template was just written
    env_path: Path


def bootstrap_workspace(root: Path) -> BootstrapResult:
    """Scaffold the workspace tree, ``.grison/`` (+ env template), and ``.gitignore``."""
    created_dirs = bootstrap_tree(root)

    grison_dir = root / ".grison"
    grison_dir.mkdir(parents=True, exist_ok=True)

    env_path = grison_dir / "env"
    env_created = False
    if not env_path.exists():
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — creds are secret
        env_created = True

    _ensure_gitignored(root, ".grison/")
    return BootstrapResult(created_dirs=created_dirs, env_created=env_created, env_path=env_path)


def _ensure_gitignored(root: Path, entry: str) -> None:
    gitignore = root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if entry not in [ln.strip() for ln in lines]:
        with gitignore.open("a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write(f"{entry}\n")
