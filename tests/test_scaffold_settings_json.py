"""``.claude/settings.json`` generation + merge (task item 4)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from grison.scaffold import settings_json as sj


def test_fresh_settings_has_deny_rules_and_hook() -> None:
    settings = sj.build_settings(None)
    deny = settings["permissions"]["deny"]
    for rule in sj.CANONICAL_DENY:
        assert rule in deny
    hooks = settings["hooks"]["PostToolUse"]
    assert any(sj._is_grison_hook_entry(h) for h in hooks)


def test_merge_preserves_unrelated_keys() -> None:
    existing = {
        "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(./.env)"]},
        "someOtherTopLevelKey": {"x": 1},
    }
    settings = sj.build_settings(existing)
    assert settings["someOtherTopLevelKey"] == {"x": 1}
    assert "Bash(npm test)" in settings["permissions"]["allow"]
    assert "Read(./.env)" in settings["permissions"]["deny"]
    for rule in sj.CANONICAL_DENY:
        assert rule in settings["permissions"]["deny"]


def test_merge_is_idempotent() -> None:
    once = sj.build_settings(None)
    twice = sj.build_settings(once)
    assert once == twice
    assert len(twice["permissions"]["deny"]) == len(sj.CANONICAL_DENY)
    assert len(twice["hooks"]["PostToolUse"]) == 1


def test_plain_run_restores_a_removed_deny_entry() -> None:
    # self-healing: these are guardrails, not preferences (see build_settings's docstring)
    settings = sj.build_settings(None)
    settings["permissions"]["deny"].remove(sj.DENY_SYNC)
    healed = sj.build_settings(settings)
    assert sj.DENY_SYNC in healed["permissions"]["deny"]


def test_force_prunes_and_recomputes_grisons_own_entries() -> None:
    settings = sj.build_settings(None)
    # a user-added, non-grison deny rule must survive a --force run untouched
    settings["permissions"]["deny"].append("Read(./secrets/**)")
    forced = sj.build_settings(settings, force=True)
    assert "Read(./secrets/**)" in forced["permissions"]["deny"]
    for rule in sj.CANONICAL_DENY:
        assert forced["permissions"]["deny"].count(rule) == 1
    assert len(forced["hooks"]["PostToolUse"]) == 1


def test_load_existing_returns_none_when_absent(tmp_path: Path) -> None:
    assert sj.load_existing(tmp_path) is None


def test_load_existing_raises_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / sj.SETTINGS_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(sj.SettingsMergeError):
        sj.load_existing(tmp_path)


def test_write_and_reload_round_trips(tmp_path: Path) -> None:
    settings = sj.build_settings(None)
    sj.write_settings(tmp_path, settings)
    loaded = sj.load_existing(tmp_path)
    assert loaded == settings
    text = (tmp_path / sj.SETTINGS_RELATIVE_PATH).read_text()
    json.loads(text)  # valid JSON
    assert text.endswith("\n")


# --- .grison/ Read-deny scope: SPEC.md/templates readable, private paths denied -----
#
# A small executable matcher for the documented gitignore-style rule semantics
# (https://code.claude.com/docs/en/permissions: "/path" is relative to the settings
# source, "**" matches any number of directories) — this is deliberately independent
# of grison's own code, so it proves the RULE TEXT itself has the intended scope
# rather than just re-asserting whatever grison.manifest.PRIVATE_ENTRIES says.

_RULE_RE = re.compile(r"^(Read|Edit)\((.*)\)$")


def _matches(rule: str, rel_path: str) -> bool:
    m = _RULE_RE.match(rule)
    assert m, rule
    spec = m.group(2)
    assert spec.startswith("/"), f"expected a settings-source-relative pattern: {rule}"
    spec = spec[1:]
    pattern = re.escape(spec).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(pattern, rel_path) is not None


def _deny_patterns(tool: str) -> list[str]:
    return [d for d in sj.CANONICAL_DENY if d.startswith(f"{tool}(")]


@pytest.mark.parametrize("rel", [".grison/SPEC.md", ".grison/templates/finding-library.md"])
def test_spec_and_templates_are_not_read_denied(rel: str) -> None:
    assert not any(_matches(r, rel) for r in _deny_patterns("Read")), rel


@pytest.mark.parametrize(
    "rel",
    [
        ".grison/env",
        ".grison/state/foo.json",
        ".grison/snapshots/2026-01-01/x.json",
        ".grison/lock",
        ".grison/terms.txt",
    ],
)
def test_private_grison_entries_are_read_denied(rel: str) -> None:
    assert any(_matches(r, rel) for r in _deny_patterns("Read")), rel


@pytest.mark.parametrize(
    "rel",
    [
        ".grison/SPEC.md",
        ".grison/templates/finding-library.md",
        ".grison/env",
        ".grison/manifest.yml",
        ".grison/index.json",
    ],
)
def test_everything_under_grison_is_edit_denied(rel: str) -> None:
    assert any(_matches(r, rel) for r in _deny_patterns("Edit")), rel
