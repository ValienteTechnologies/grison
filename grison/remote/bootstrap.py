"""First-run scaffolding — the binary sets everything up; the human pastes creds.

Any verb in a fresh dir creates the workspace tree, ``.grison/`` (grison's private
dir, like ``.git/`` — always gitignored so creds never commit), a commented
``.grison/env`` template (chmod 600), and the ``.gitignore`` entry. There is no
``init`` and no interactive wizard: template + one message is deterministic
everywhere. ``parse`` is fully offline; ``sync`` additionally requires the creds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from grison import manifest as manifest_mod
from grison.fsio import atomic_write_text, ensure_private_dir
from grison.index import Index
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
    # recursive=True: tighten permissions of files/dirs that ALREADY exist under
    # .grison/ on every run too (a hand-created .grison/env with a wide mode, or a
    # .grison/ copied/extracted from elsewhere, must not stay wide just because
    # bootstrap only ever chmod'd the directory itself before).
    ensure_private_dir(grison_dir, recursive=True)

    env_path = grison_dir / "env"
    env_created = False
    if not env_path.exists():
        atomic_write_text(env_path, _ENV_TEMPLATE, private=True)  # creds are secret
        env_created = True

    # A brand-new workspace (never had v1 content) starts at the CURRENT format —
    # manifest.read() would otherwise read it as v1 the moment .grison/env exists
    # (v1 predates manifest.yml entirely; see manifest.py's own docstring), sending
    # a workspace that never had any v1 data through the "needs migration" path on
    # its very first sync. This isn't only the freshly-generated-env case: a
    # workspace whose .grison/env was created by hand or copied in (a scripted
    # deployment, a credential rotation, this very repo's own lab proof) never sets
    # env_created either, so the real signal is "is there any actual v1 CONTENT
    # anywhere" (findings/ or methodology/ has at least one real file already) —
    # only THAT means a real pre-v2 workspace whose format the later migration step
    # must convert, not this bootstrap. Format v2's own .gitignore rule (D13/
    # "Workspace format v2"): .grison/.gitignore is the private-file allow-list; the
    # workspace root's OWN .gitignore must NOT blanket-ignore .grison/ (that was the
    # v1 scaffold's shape) since manifest.yml/index.json must stay tracked.
    manifest_path = root / ".grison" / "manifest.yml"
    has_v1_content = any(
        d.is_dir() and any(p.is_file() for p in d.rglob("*"))
        for d in (root / "findings", root / "methodology")
        if d.is_dir()
    )
    if not manifest_path.exists() and not has_v1_content:
        manifest_mod.write(root)
        manifest_mod.write_gitignore(root)
        index_path = root / ".grison" / "index.json"
        if not index_path.exists():
            Index(root=root).save()

    settings = load_settings(root)
    claude_md_path = root / "CLAUDE.md"
    claude_md_created = False
    if settings.claude_md_enabled and not claude_md_path.exists():
        atomic_write_text(claude_md_path, _CLAUDE_MD_TEMPLATE)  # tracked, not private
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
