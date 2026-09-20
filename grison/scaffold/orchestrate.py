"""``scaffold_workspace`` — the one entry point that (re)generates every
self-contained-workspace artifact the brief's D11/D12/D13 items name:
``.grison/SPEC.md``, ``.grison/templates/``, ``.grison/terms.txt``, ``CLAUDE.md``,
``.claude/settings.json``, the workspace-root ``.gitignore``'s collision-sidecar
entry, and (when the workspace is a git repository) the ``pre-commit`` hook.

Called from two places: :func:`grison.remote.bootstrap.bootstrap_workspace` (every
``grison sync``/``grison parse``, so a first run in an empty directory yields a
complete, self-contained workspace) and ``grison scaffold [--force]`` (on demand,
re-running the same logic — ``--force`` additionally lets the idempotent parts that
support it (``.claude/settings.json``, the root ``.gitignore``, the pre-commit hook,
``CLAUDE.md``) recompute themselves from scratch instead of only ever adding).

Every path this module ever writes ALSO gets its digest recorded via
:func:`grison.validator.mirrors.record_digest`, so a future validator rule for "was
this hand-edited since grison generated it" (WS-009's own mechanism) only needs the
read side (:mod:`grison.validator.core` isn't touched here — see this task's report).
The exact paths recorded: ``.grison/SPEC.md`` (every run), ``.grison/templates/
finding-library.md``/``finding-reported.md``/``wiki-page.md``/``project-note.md``
(only the run that first writes each one), and ``CLAUDE.md`` (every run that
(re)writes it — see :func:`_scaffold_claude_md`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from grison.fsio import atomic_write_text
from grison.hashing import digest_text
from grison.remote.creds import Settings
from grison.scaffold import claude_md as claude_md_mod
from grison.scaffold import gitignore as gitignore_mod
from grison.scaffold import precommit as precommit_mod
from grison.scaffold import settings_json as settings_json_mod
from grison.scaffold import spec as spec_mod
from grison.scaffold import templates as templates_mod
from grison.scaffold import terms as terms_mod
from grison.validator import mirrors as mirrors_mod

CLAUDE_MD_RELATIVE_PATH = "CLAUDE.md"


@dataclass
class ScaffoldResult:
    spec_written: bool = False
    templates_written: list[str] = field(default_factory=list)
    terms_written: bool = False
    gitignore_updated: bool = False
    settings_updated: bool = False
    claude_md_status: str = "disabled"  # created | regenerated | up-to-date | stale-hand-edited
    # | disabled
    precommit: precommit_mod.PrecommitResult | None = None


def _scaffold_spec(root: Path) -> bool:
    """``.grison/SPEC.md`` — grison-owned, always kept current (it carries no
    authored content an agent could have edited on purpose; unlike ``CLAUDE.md`` there
    is no "hand-edited, leave it alone" case for it). Its digest is checked on every
    run so a validator rule always has an up-to-date baseline to compare a hand-edit
    against — see :mod:`grison.validator.mirrors` — but ``record_digest`` only
    actually rewrites ``mirrors.json`` when the digest changed, so a run that changes
    nothing here touches nothing on disk."""
    path = root / spec_mod.SPEC_RELATIVE_PATH
    text = spec_mod.spec_text()
    written = not (path.exists() and path.read_text(encoding="utf-8") == text)
    if written:
        atomic_write_text(path, text)
    mirrors_mod.record_digest(root, spec_mod.SPEC_RELATIVE_PATH, digest_text(text))
    return written


def _scaffold_templates(root: Path) -> list[str]:
    """Writes every template that doesn't already exist. Templates are starting
    points an agent is expected to copy and edit — once one exists, grison never
    touches it again (even with ``--force``: overwriting a template an operator
    customized would be exactly the kind of silent clobber D11 exists to prevent).
    Each template's digest is recorded ONLY at the moment it is freshly written (the
    "known-good, as-generated" baseline a future validator rule could compare a
    template's current content against) — never re-recorded against an existing
    template's on-disk content, which could already be a deliberate customization."""
    written: list[str] = []
    for name, content in templates_mod.all_templates().items():
        path = root / templates_mod.TEMPLATES_RELATIVE_DIR / name
        if not path.exists():
            atomic_write_text(path, content)
            mirrors_mod.record_digest(
                root, f"{templates_mod.TEMPLATES_RELATIVE_DIR}/{name}", digest_text(content)
            )
            written.append(name)
    return written


def _scaffold_claude_md(root: Path, *, force: bool, enabled: bool) -> str:
    path = root / CLAUDE_MD_RELATIVE_PATH
    if not enabled:
        return "disabled"
    text = claude_md_mod.build_claude_md()
    expected_marker = claude_md_mod.marker_line()

    def _write() -> None:
        atomic_write_text(path, text)
        mirrors_mod.record_digest(root, CLAUDE_MD_RELATIVE_PATH, digest_text(text))

    if not path.exists():
        _write()
        return "created"

    current = path.read_text(encoding="utf-8")
    if force:
        _write()
        return "regenerated"

    recorded = mirrors_mod.expected_digest(root, CLAUDE_MD_RELATIVE_PATH)
    unmodified = recorded is not None and digest_text(current) == recorded
    current_marker = current.splitlines()[0] if current else ""
    is_current = current_marker == expected_marker

    if is_current:
        return "up-to-date"
    if unmodified:
        _write()
        return "regenerated"
    print(
        f"grison: {path} is stale (doesn't match what this grison/spec version would "
        "generate) and isn't exactly what grison last generated, so it looks "
        "hand-edited — leaving it alone. Run `grison scaffold --force` to regenerate "
        "it (this discards any hand edits).",
        file=sys.stderr,
    )
    return "stale-hand-edited"


def scaffold_workspace(
    root: Path, *, force: bool = False, settings: Settings | None = None
) -> ScaffoldResult:
    """(Re)generate every scaffolded file under ``root``. Safe to call on every
    ``grison sync``/``grison parse`` (idempotent, self-healing for the guardrail
    pieces — see each helper's own docstring for exactly what ``force`` changes) and
    on demand via ``grison scaffold``."""
    if settings is None:
        from grison.remote.creds import load_settings

        settings = load_settings(root)

    result = ScaffoldResult()
    result.spec_written = _scaffold_spec(root)
    result.templates_written = _scaffold_templates(root)
    result.terms_written = terms_mod.scaffold_terms(root)

    gitignore_mod.ensure_gitignore(root, force=force)
    result.gitignore_updated = True

    existing_settings = settings_json_mod.load_existing(root)
    merged = settings_json_mod.build_settings(existing_settings, force=force)
    if merged != existing_settings:
        settings_json_mod.write_settings(root, merged)
        result.settings_updated = True
    # Baseline for WS-011/WS-012 (grison.validator.core): the merged settings ALWAYS
    # carry the full canonical deny set + hook entry (build_settings self-heals them
    # in, whether or not this run actually rewrote the file) — see
    # grison.scaffold.settings_json.fragment_digest's own docstring for exactly what
    # this baseline covers (never the user's own additions).
    merged_deny = merged.get("permissions", {}).get("deny", [])
    merged_hooks = merged.get("hooks", {}).get("PostToolUse", [])
    mirrors_mod.record_digest(
        root,
        settings_json_mod.SETTINGS_RELATIVE_PATH,
        settings_json_mod.fragment_digest(merged_deny, merged_hooks),
    )

    result.claude_md_status = _scaffold_claude_md(
        root, force=force, enabled=settings.claude_md_enabled
    )

    result.precommit = precommit_mod.install_precommit_hook(root, force=force)

    return result
