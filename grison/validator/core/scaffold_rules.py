"""WS-011/WS-012: the D11 self-contained-workspace files ``grison scaffold``
generates/merges, checked for presence and (where content drift is real corruption
rather than intended editing) digest agreement."""

from __future__ import annotations

from pathlib import Path

from grison.hashing import digest_text
from grison.validator import registry
from grison.validator.mirrors import expected_digest
from grison.validator.registry import Failure, fail


def _check_scaffolded_files(root: Path) -> list[Failure]:  # WS-011/WS-012
    """The D11 self-contained-workspace files ``grison scaffold`` generates/merges:
    ``.grison/SPEC.md``, each ``.grison/templates/*.md``, ``CLAUDE.md``,
    ``.claude/settings.json``. For every one of them, ``expected_digest`` being
    ``None`` means this grison has never generated it in THIS workspace — never a
    failure (brief D3/D13: a fresh-but-not-yet-scaffolded workspace is not corrupt;
    the same rule the sync-time mirrors already follow, and why none of the plain
    ``tests/fixtures/ws-v2`` validator tests — which never run ``grison scaffold`` —
    ever hit this function). Once a digest IS recorded: missing -> WS-011; present but
    not matching -> WS-012, with what "matching" means chosen per file so a user's own
    ADDITIONS are never flagged (see each branch, and
    :func:`grison.scaffold.settings_json.fragment_digest`'s own docstring for the
    settings.json/CLAUDE.md rationale in full):

    - ``.grison/SPEC.md``: exact content digest (no authored content ever belongs
      here — same as a sync-time mirror, WS-009's own rule).
    - a template: existence only, never content — the whole point of a template
      (``grison.scaffold.orchestrate``'s own docstring) is that an agent copies and
      edits it once created; grison never touches it again, so content drift is the
      INTENDED use, not corruption.
    - ``CLAUDE.md``: exact digest of the full text AS LAST (RE)GENERATED, but only
      when its own marker line has also drifted from what THIS grison would generate
      now — mirrors exactly ``grison.scaffold.orchestrate._scaffold_claude_md``'s own
      "stale-hand-edited" condition (the one case it leaves alone with a warning
      instead of silently regenerating): a current-marker CLAUDE.md is never flagged
      regardless of what's below the marker (an agent/operator extending it is the
      documented way to use it), only a STALE copy whose content also drifted from
      what was last recorded is invalid.
    - ``.claude/settings.json``: the grison-owned fragment only (canonical deny rules
      present + hook-entry present) — a user's own extra deny/allow rules or other
      top-level keys never change this fragment's digest.
    """
    import json

    from grison.scaffold import claude_md as claude_md_mod
    from grison.scaffold import settings_json as settings_json_mod
    from grison.scaffold import spec as spec_mod
    from grison.scaffold import templates as templates_mod
    from grison.scaffold.orchestrate import CLAUDE_MD_RELATIVE_PATH

    out: list[Failure] = []

    def _missing_or(rel: str, ok: bool) -> bool:
        """``True`` and appends WS-011 if ``rel`` is missing and had a recorded
        digest; ``False`` (nothing appended, caller proceeds to its own content
        check) otherwise. ``ok`` is whether ``rel`` currently exists."""
        if not ok:
            out.append(
                fail(registry.WS_SCAFFOLD_MISSING, rel, "grison generated this; it is now missing")
            )
        return not ok

    # .grison/SPEC.md — exact content match, like a sync-time mirror.
    spec_digest = expected_digest(root, spec_mod.SPEC_RELATIVE_PATH)
    if spec_digest is not None:
        spec_path = root / spec_mod.SPEC_RELATIVE_PATH
        if not _missing_or(spec_mod.SPEC_RELATIVE_PATH, spec_path.is_file()):
            text = spec_path.read_text(encoding="utf-8", errors="replace")
            if digest_text(text) != spec_digest:
                out.append(
                    fail(
                        registry.WS_SCAFFOLD_EDITED,
                        spec_mod.SPEC_RELATIVE_PATH,
                        "content differs from what grison last generated",
                    )
                )

    # .grison/templates/*.md — existence only (see docstring: content drift is
    # intended usage, not corruption).
    for name in templates_mod.all_templates():
        rel = f"{templates_mod.TEMPLATES_RELATIVE_DIR}/{name}"
        if expected_digest(root, rel) is not None:
            _missing_or(rel, (root / rel).is_file())

    # CLAUDE.md — only flagged when ALSO stale against the current generator (see
    # docstring): mirrors _scaffold_claude_md's own "stale-hand-edited" condition.
    claude_digest = expected_digest(root, CLAUDE_MD_RELATIVE_PATH)
    if claude_digest is not None:
        claude_path = root / CLAUDE_MD_RELATIVE_PATH
        if not _missing_or(CLAUDE_MD_RELATIVE_PATH, claude_path.is_file()):
            current = claude_path.read_text(encoding="utf-8", errors="replace")
            current_marker = current.splitlines()[0] if current else ""
            is_current = current_marker == claude_md_mod.marker_line()
            if not is_current and digest_text(current) != claude_digest:
                out.append(
                    fail(
                        registry.WS_SCAFFOLD_EDITED,
                        CLAUDE_MD_RELATIVE_PATH,
                        "stale (older than this grison would generate now) and its "
                        "content no longer matches what grison last generated",
                    )
                )

    # .claude/settings.json — grison-owned fragment only (canonical deny rules
    # present + hook-entry present); a user's own additions never change the digest.
    settings_digest = expected_digest(root, settings_json_mod.SETTINGS_RELATIVE_PATH)
    if settings_digest is not None:
        settings_path = root / settings_json_mod.SETTINGS_RELATIVE_PATH
        if not _missing_or(settings_json_mod.SETTINGS_RELATIVE_PATH, settings_path.is_file()):
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            deny = (data.get("permissions") or {}).get("deny", []) if isinstance(data, dict) else []
            post_tool_use = (
                (data.get("hooks") or {}).get("PostToolUse", []) if isinstance(data, dict) else []
            )
            live_digest = settings_json_mod.fragment_digest(
                deny if isinstance(deny, list) else [],
                post_tool_use if isinstance(post_tool_use, list) else [],
            )
            if live_digest != settings_digest:
                out.append(
                    fail(
                        registry.WS_SCAFFOLD_EDITED,
                        settings_json_mod.SETTINGS_RELATIVE_PATH,
                        "grison's own deny rules/post-edit hook no longer match what grison last "
                        "merged in",
                    )
                )

    return out
