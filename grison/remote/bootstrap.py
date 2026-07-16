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

from grison.remote.creds import load_settings
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

# Cloudflare Access service token — only if the deployment sits behind CF Access;
# leave both empty otherwise
GRISON_CF_CLIENT_ID=
GRISON_CF_CLIENT_SECRET=

# grison behavior — non-secret settings, same precedence as the creds above.
# GRISON_GIT=commit     # let grison checkpoint + commit the tree around sync/parse (default: off)
# GRISON_CLAUDE_MD=off  # skip scaffolding CLAUDE.md operator notes on first bootstrap (default: on)
"""

_CLAUDE_MD_TEMPLATE = """\
# grison workspace — operator notes

This is a grison workspace: a plain-markdown mirror of Ghostwriter findings/reports
and BookStack methodology. Editing the markdown here IS how you change the remote
data — grison only validates and syncs, it has no AI subsystem of its own. You (the
agent) are the transform layer.

## Layout

- `findings/inbox/` — `grison parse` output, local-only. Triage here: read, edit,
  then `cp`/`mv` the keepers into `findings/library/` or a report dir. Never synced.
- `findings/library/` — reusable finding templates. Syncs to Ghostwriter's finding
  library.
- `findings/reports/<id>-<slug>/` — one dir per *existing* Ghostwriter report.
  grison never creates reports. Findings placed directly here sync as that report's
  reported findings.
- `findings/reports/<id>-<slug>/narrative/` — one markdown file per report
  narrative section (exec summary, methodology, …). Edit freely; 3-way merged
  per section.
- `methodology/library/<book>/<chapter>/` — BookStack pages, markdown-native,
  mirrored verbatim both ways.
- `methodology/checklists/<engagement>/` — per-engagement working copies (`cp -r`
  from library). Local-only, never synced.

## Frontmatter contract

Every finding/page has a `grison:` block in its YAML frontmatter (ids, hashes, sync
state). **Never hand-edit anything inside `grison:`** — it's machine-owned and is the
3-way merge base. Everything else (title, severity, body prose, tags, CVSS, …) is
yours to edit normally.

## `.grison/`

grison's private state directory — creds, sync state, snapshots. Never read or write
anything under here; it isn't part of the workspace data model.

## Report dirs — what's read-only

- `.report.yml` and `project.md` are regenerated every sync — read-only mirrors of
  Ghostwriter project metadata. Read `project.md` for engagement context (scope,
  objectives, white cards) before writing narrative — don't edit it.
- `notes/<id>.md` files (with a `grison:` id in frontmatter) are read-only mirrors
  of Ghostwriter project notes. To share a new note with the team, create
  `notes/<name>.md` **without** frontmatter — grison pushes it as a new note on the
  next sync.

## Commands

- `grison parse <file>` — scanner export → `findings/inbox/*.md` (offline).
- `grison status <path…>` — validity report (offline, no writes).
- `grison sync` / `grison sync --dry-run` — reconcile with Ghostwriter + BookStack;
  dry-run previews the plan without writing anything.

## Scope discipline

Work only within the scope entries listed in each report's `project.md`. Entries
marked `EXCLUDED` are off-limits — do not create findings, evidence, or narrative
referencing them.
"""


@dataclass
class BootstrapResult:
    created_dirs: list[Path]
    env_created: bool  # True if a fresh (unfilled) env template was just written
    env_path: Path
    claude_md_created: bool  # True if a fresh CLAUDE.md scaffold was just written


def bootstrap_workspace(root: Path) -> BootstrapResult:
    """Scaffold the workspace tree, ``.grison/`` (+ env template), ``.gitignore``, and
    (unless disabled) a ``CLAUDE.md`` operator-notes scaffold."""
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

    settings = load_settings(root)
    claude_md_path = root / "CLAUDE.md"
    claude_md_created = False
    if settings.claude_md_enabled and not claude_md_path.exists():
        claude_md_path.write_text(_CLAUDE_MD_TEMPLATE, encoding="utf-8")
        claude_md_created = True

    return BootstrapResult(
        created_dirs=created_dirs,
        env_created=env_created,
        env_path=env_path,
        claude_md_created=claude_md_created,
    )


def _ensure_gitignored(root: Path, entry: str) -> None:
    gitignore = root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if entry not in [ln.strip() for ln in lines]:
        with gitignore.open("a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write(f"{entry}\n")
