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
from grison.scaffold import ScaffoldResult, scaffold_workspace
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


@dataclass
class BootstrapResult:
    created_dirs: list[Path]
    env_created: bool  # True if a fresh (unfilled) env template was just written
    env_path: Path
    claude_md_created: bool  # True if a fresh CLAUDE.md scaffold was just written
    scaffold: ScaffoldResult  # every other scaffolded file (SPEC.md, templates/,
    # terms.txt, .claude/settings.json, root .gitignore, the git pre-commit hook) —
    # see grison.scaffold.orchestrate.scaffold_workspace


def bootstrap_workspace(root: Path, *, bootstrapped: bool | None = None) -> BootstrapResult:
    """Scaffold the workspace tree, ``.grison/`` (+ env template), and every
    self-contained-workspace artifact :mod:`grison.scaffold` owns — ``.grison/SPEC.md``,
    ``.grison/templates/``, ``.grison/terms.txt``, ``CLAUDE.md``,
    ``.claude/settings.json``, the root ``.gitignore``'s collision-sidecar entry, and
    (when this is a git repo) the ``pre-commit`` hook — so a first ``grison sync``/
    ``grison parse`` in an empty directory yields a complete, valid, self-contained
    workspace (brief D11).

    ``bootstrapped``: pass the caller's own already-computed
    :func:`grison.manifest.is_bootstrapped` result to skip recomputing it here —
    see :func:`grison.cli.guards._refuse_if_format_mismatch`'s docstring for why
    (its ``has_v1_content`` half walks findings/methodology with an ``rglob``).
    Left ``None``, it's computed fresh."""
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
    # a workspace that never had any v1 data through the "refused, wrong format"
    # path on its very first sync. This isn't only the freshly-generated-env case: a
    # workspace whose .grison/env was created by hand or copied in (a scripted
    # deployment, a credential rotation, this very repo's own lab proof) never sets
    # env_created either, so the real signal is "is there any actual v1 CONTENT
    # anywhere" (findings/ or methodology/ has at least one real file already) —
    # only THAT means a real pre-v2 workspace, which D13 refuses outright (no
    # migration converts it) rather than this bootstrap writing a v2 manifest over
    # it. Format v2's own .gitignore rule (D13/"Workspace format v2"):
    # .grison/.gitignore is the private-file allow-list; the workspace root's OWN
    # .gitignore must NOT blanket-ignore .grison/ (that was the v1 scaffold's
    # shape) since manifest.yml/index.json must stay tracked.
    manifest_path = root / ".grison" / "manifest.yml"
    if bootstrapped is None:
        bootstrapped = manifest_mod.is_bootstrapped(root)
    if not bootstrapped:
        manifest_mod.write(root)
        manifest_mod.write_gitignore(root)
        index_path = root / ".grison" / "index.json"
        if not index_path.exists():
            Index(root=root).save()

    settings = load_settings(root)
    # A real v1 workspace (has_v1_content, no manifest.yml written above) must not
    # get v2-shaped scaffolding — CLAUDE.md's frontmatter rules, .grison/SPEC.md,
    # and the rest all describe format v2, which this workspace is permanently
    # refused for (D13: no migration converts it). `manifest_path` now exists
    # exactly when this IS (or just became, in the block above) a v2 workspace.
    if manifest_path.exists():
        scaffold = scaffold_workspace(root, settings=settings)
    else:
        scaffold = ScaffoldResult()

    return BootstrapResult(
        created_dirs=created_dirs,
        env_created=env_created,
        env_path=env_path,
        claude_md_created=scaffold.claude_md_status == "created",
        scaffold=scaffold,
    )
