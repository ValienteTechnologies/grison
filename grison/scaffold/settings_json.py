"""``.claude/settings.json`` — Claude Code project settings, generated + merged
idempotently (brief D11).

Schema references (fetched 2026-09-17; module docstring cites them rather than
reproducing them, since they can change):
  - permission rule syntax (``Tool``/``Tool(specifier)``, gitignore-style path
    patterns, the ``/path`` "relative to the settings source" form, wildcards):
    https://code.claude.com/docs/en/permissions
  - settings file shape and precedence: https://code.claude.com/docs/en/settings
  - hook event/matcher/handler shape, the ``PostToolUse`` stdin/stdout contract, and
    exit-code semantics: https://code.claude.com/docs/en/hooks

Two documented facts drive the shape below:

1. "Claude Code checks file permissions against ``Edit(path)`` and ``Read(path)``
   rules only" — a ``Write(...)``/``MultiEdit(...)`` path rule is accepted but never
   consulted. A ``Read`` deny additionally blocks the Edit/Write tools on the same
   path (creating a new file included); an ``Edit`` deny blocks Edit/Write but NOT
   reads. ``.grison/`` is NOT uniformly secret: ``.grison/SPEC.md`` and
   ``.grison/templates/`` exist precisely so an agent can read the full spec and copy
   a template (see ``CLAUDE.md``) — only the PRIVATE entries
   (:data:`grison.manifest.PRIVATE_ENTRIES`: ``env``, ``state/``, ``snapshots/``,
   ``lock``, ``terms.txt``) get a ``Read`` deny; everything under ``.grison/`` (tracked
   or private alike) gets an ``Edit`` deny, since an agent never WRITES anything there
   regardless of whether it may read it. The read-only mirrors elsewhere in the
   workspace (``project.md`` etc.) get the same ``Edit``-only treatment for the same
   reason: readable for context, never editable.
2. "When a command redirects output... Claude Code checks the redirect target against
   your ``Edit`` allow and deny rules as if Claude wrote that file directly." Since
   the blanket ``Edit(/.grison/**)`` deny below covers every path under ``.grison/``,
   this ALSO stops a Bash redirect like ``echo x > .grison/env`` with no separate Bash
   rule needed.

For the ``grison sync``/``grison undo`` denial: a Bash deny rule matches literal
command TEXT, not the underlying program, so it is not a sandboxing boundary — the
docs are explicit that ``Bash(rm *)`` does not stop ``/bin/rm`` or ``sh -c 'rm ...'``.
Rather than enumerate spellings (``python -m grison sync``, ``uv run grison sync``,
``uvx grison sync``, an absolute path to the binary, ...), one substring-wildcard rule
per subcommand — ``Bash(*grison sync*)``, ``Bash(*grison undo*)`` — matches every one
of those, since each contains the literal text ``grison sync``/``grison undo``
somewhere in the command, while `*` matches any (possibly empty) text on both sides.
This also denies every ``--force-local``/``--force-remote`` spelling and plain
``grison sync`` in one rule, since they're all supersets of the same substring. This
is advisory hardening, not a security boundary (see the docs' own "Bash permission
patterns that try to constrain arguments are fragile" warning) — the real backstops
are the git pre-commit hook and the owner-only nature of ``grison sync``/``undo``
themselves.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from grison.errors import GrisonError
from grison.fsio import atomic_write_text
from grison.manifest import PRIVATE_ENTRIES

SETTINGS_RELATIVE_PATH = ".claude/settings.json"


def _read_deny_pattern(entry: str) -> str:
    """``Read(/.grison/<entry>)`` for a file, ``Read(/.grison/<entry>/**)`` for a
    directory (:data:`grison.manifest.PRIVATE_ENTRIES` marks a directory with a
    trailing ``/``, the same convention ``TRACKED_ENTRIES`` uses)."""
    if entry.endswith("/"):
        return f"Read(/.grison/{entry}**)"
    return f"Read(/.grison/{entry})"


# One Read deny per PRIVATE entry (never a blanket .grison/ Read deny — SPEC.md and
# templates/ must stay readable, see the module docstring) + one blanket Edit deny for
# ALL of .grison/ (nothing under it, tracked or private, is ever agent-writable).
DENY_GRISON_PRIVATE_READ: tuple[str, ...] = tuple(
    _read_deny_pattern(entry) for entry in PRIVATE_ENTRIES
)
DENY_GRISON_WRITE_ALL = "Edit(/.grison/**)"
DENY_MIRRORS: tuple[str, ...] = (
    "Edit(/**/.report.yml)",
    "Edit(/**/project.md)",
    "Edit(/**/.book.yml)",
    "Edit(/**/.chapter.yml)",
    "Edit(/**/.shelves/**)",
)
DENY_SYNC = "Bash(*grison sync*)"
DENY_UNDO = "Bash(*grison undo*)"

CANONICAL_DENY: tuple[str, ...] = (
    *DENY_GRISON_PRIVATE_READ, DENY_GRISON_WRITE_ALL, *DENY_MIRRORS, DENY_SYNC, DENY_UNDO,
)

HOOK_MATCHER = "Edit|Write|MultiEdit"
# A tiny inline guard, not a separate script (see the brief's own "prefer the
# subcommand: ... testable in Python" steer) — `grison hook post-edit` does all the
# real work; this only keeps the hook from ever failing loudly when grison isn't on
# PATH, and always exits 0 (PostToolUse can't block anyway — see the module docstring
# — but a clean, silent exit 0 either way is what "never blocks" means in practice).
HOOK_COMMAND = (
    "if command -v grison >/dev/null 2>&1; then grison hook post-edit; "
    "else echo 'grison: not installed — skipping post-edit validation' >&2; fi; exit 0"
)


class SettingsMergeError(GrisonError, ValueError):
    """An existing ``.claude/settings.json`` could not be safely merged — grison
    never guesses at a malformed file; it reports the problem and leaves it alone."""


def _canonical_hook_entry() -> dict[str, Any]:
    return {
        "matcher": HOOK_MATCHER,
        "hooks": [{"type": "command", "command": HOOK_COMMAND}],
    }


def _is_grison_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or entry.get("matcher") != HOOK_MATCHER:
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and h.get("type") == "command" and h.get("command") == HOOK_COMMAND
        for h in hooks
    )


def load_existing(root: Path) -> dict[str, Any] | None:
    """The existing ``.claude/settings.json``, or ``None`` if there isn't one. Raises
    :class:`SettingsMergeError` for a file that exists but isn't a JSON object —
    grison must never silently overwrite something it can't parse."""
    path = root / SETTINGS_RELATIVE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsMergeError(f"{path}: could not parse existing settings: {e}") from e
    if not isinstance(data, dict):
        raise SettingsMergeError(f"{path}: top level must be a JSON object")
    return data


def build_settings(existing: dict[str, Any] | None, *, force: bool = False) -> dict[str, Any]:
    """Merge grison's deny rules + post-edit hook into ``existing`` (or start fresh),
    idempotently: every OTHER key, and every deny/hook entry not recognizably
    grison's own, is passed through untouched — grison never clobbers the user's own
    settings, only ensures its own guardrails are present among them.

    These are safety guardrails (D11), not preferences: a plain (non-``force``) run
    always ensures the current canonical entries are present, self-healing a copy an
    agent (or a careless hand-edit) removed, exactly like the git pre-commit hook and
    the workspace's other enforcement points aren't something a workspace edit can
    quietly turn off. ``force=True`` additionally PRUNES any entry this module
    recognizes as its own before re-adding the current set — the hook a plain run
    can't perform, since it only ever adds — so a wording change in a future grison
    version replaces what an older one wrote instead of leaving both side by side.
    """
    settings: dict[str, Any] = copy.deepcopy(existing) if existing else {}

    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SettingsMergeError("'permissions' must be an object")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise SettingsMergeError("'permissions.deny' must be an array")
    if force:
        deny[:] = [d for d in deny if d not in CANONICAL_DENY]
    for rule in CANONICAL_DENY:
        if rule not in deny:
            deny.append(rule)

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsMergeError("'hooks' must be an object")
    post_tool_use = hooks.setdefault("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        raise SettingsMergeError("'hooks.PostToolUse' must be an array")
    if force:
        post_tool_use[:] = [h for h in post_tool_use if not _is_grison_hook_entry(h)]
    if not any(_is_grison_hook_entry(h) for h in post_tool_use):
        post_tool_use.append(_canonical_hook_entry())

    return settings


def write_settings(root: Path, settings: dict[str, Any]) -> None:
    atomic_write_text(root / SETTINGS_RELATIVE_PATH, json.dumps(settings, indent=2) + "\n")
