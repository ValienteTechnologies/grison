"""Failing-case tests for WS-001..WS-012 (workspace layout, hygiene, format version).
Passing cases mostly live in ``test_validator_fixture.py``; the ones needing their own
setup (git hygiene, a recorded mirror digest) are here too."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from grison.hashing import digest_text
from grison.remote.bootstrap import bootstrap_workspace
from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids


@pytest.mark.rule("WS-001")
def test_ws001_bad_file_name(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    bad = root / "findings" / "library" / "Weak TLS.md"
    (root / "findings" / "library" / "weak-tls-config.md").rename(bad)
    fails = validate_workspace(root)
    assert "WS-001" in rule_ids(fails)


@pytest.mark.rule("WS-002")
def test_ws002_unknown_findings_path(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "findings" / "reports" / "14-acme-corp" / "scratch.txt").write_text("stray\n")
    fails = validate_workspace(root)
    assert "WS-002" in rule_ids(fails)


@pytest.mark.rule("WS-003")
def test_ws003_unknown_methodology_path(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "methodology" / "scratch.txt").write_text("stray\n")
    fails = validate_workspace(root)
    assert "WS-003" in rule_ids(fails)


@pytest.mark.rule("WS-003")
def test_ws003_unknown_shelves_entry(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "methodology" / "library" / ".shelves" / "scratch.txt").write_text("stray\n")
    fails = validate_workspace(root)
    assert "WS-003" in rule_ids(fails)


def test_report_dir_name_is_a_stable_handle_not_derived_data(tmp_path: Path) -> None:
    """WS-004 is retired (see registry.py) — a report directory's name carries no
    required shape at all beyond WS-001's charset; renaming '14-acme-corp' to a bare,
    unprefixed 'acme-corp' (exactly what a fresh v2 pull produces via slug(title))
    must NOT fail, and IDX-003 (not a name-shape rule) is what fires when the
    directory stops being indexed."""
    root = copy_fixture(tmp_path)
    (root / "findings" / "reports" / "14-acme-corp").rename(
        root / "findings" / "reports" / "acme-corp"
    )
    data = json.loads((root / ".grison" / "index.json").read_text(encoding="utf-8"))
    old_prefix = "findings/reports/14-acme-corp"
    new_prefix = "findings/reports/acme-corp"
    data["records"] = {
        (new_prefix + k[len(old_prefix) :] if k.startswith(old_prefix) else k): v
        for k, v in data["records"].items()
    }
    (root / ".grison" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    fails = validate_workspace(root)
    assert not any(f.path.startswith(new_prefix) for f in fails), fails

    # now de-index it — IDX-003 fires, not any WS- name-shape rule
    del data["records"][new_prefix]
    (root / ".grison" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    fails2 = validate_workspace(root)
    assert "IDX-003" in rule_ids(fails2)
    assert not any(f.rule_id.startswith("WS-") and f.path.startswith(new_prefix) for f in fails2)


@pytest.mark.rule("WS-005")
def test_ws005_needs_migration(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".grison" / "manifest.yml").write_text("format: 1\n")
    fails = validate_workspace(root)
    assert "WS-005" in rule_ids(fails)


@pytest.mark.rule("WS-006")
def test_ws006_too_new(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".grison" / "manifest.yml").write_text("format: 99\n")
    fails = validate_workspace(root)
    assert "WS-006" in rule_ids(fails)


@pytest.mark.rule("WS-007")
def test_ws007_bad_manifest(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".grison" / "manifest.yml").write_text("not_format: 2\n")
    fails = validate_workspace(root)
    assert "WS-007" in rule_ids(fails)


@pytest.mark.rule("WS-008")
def test_ws008_private_path_not_ignored(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    (root / ".grison" / "env").write_text("GRISON_GW_TOKEN=secret\n")  # no .gitignore rule
    fails = validate_workspace(root)
    assert "WS-008" in rule_ids(fails)


@pytest.mark.rule("WS-008")
def test_ws008_tracked_path_ignored(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    (root / ".gitignore").write_text(".grison/manifest.yml\n")
    fails = validate_workspace(root)
    assert "WS-008" in rule_ids(fails)


@pytest.mark.rule("WS-009")
def test_ws009_mirror_hand_edited(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    report_yml = root / "findings" / "reports" / "14-acme-corp" / ".report.yml"
    original = report_yml.read_text(encoding="utf-8")
    state_dir = root / ".grison" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rel = "findings/reports/14-acme-corp/.report.yml"
    (state_dir / "mirrors.json").write_text(json.dumps({rel: digest_text(original)}))
    # clean first: digest matches, no WS-009 yet
    assert "WS-009" not in rule_ids(validate_workspace(root))
    report_yml.write_text(original.replace("Acme Corp", "Hand-Edited Corp"))
    fails = validate_workspace(root)
    assert "WS-009" in rule_ids(fails)


@pytest.mark.rule_ok("WS-009")
def test_ws009_mirror_matching_recorded_digest_passes(tmp_path: Path) -> None:
    """A real positive proof (not just absence): the digest IS recorded, and the file
    on disk IS exactly what was recorded — the check must pass, not just skip."""
    root = copy_fixture(tmp_path)
    report_yml = root / "findings" / "reports" / "14-acme-corp" / ".report.yml"
    original = report_yml.read_text(encoding="utf-8")
    state_dir = root / ".grison" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rel = "findings/reports/14-acme-corp/.report.yml"
    (state_dir / "mirrors.json").write_text(json.dumps({rel: digest_text(original)}))
    assert "WS-009" not in rule_ids(validate_workspace(root))


@pytest.mark.rule("WS-010")
def test_ws010_mirror_malformed(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    book_yml = root / "methodology" / "library" / "web-application-testing" / ".book.yml"
    edit(book_yml, "name: Web Application Testing", "name: Web Application Testing\nbogus: 1")
    fails = validate_workspace(root)
    assert "WS-010" in rule_ids(fails)


def test_git_hygiene_noop_outside_a_repo(tmp_path: Path) -> None:
    """Not itself rule-marked (a negative-space proof, not a rule): confirms
    check_git_hygiene is a true no-op — not a false pass — outside any git repo."""
    root = copy_fixture(tmp_path, git=False)
    assert subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode != 0
    fails = validate_workspace(root)
    assert "WS-008" not in rule_ids(fails)


# ---------------------------------------------------------------------------
# WS-011/WS-012: the D11 scaffolded/merged files (.grison/SPEC.md, a
# .grison/templates/*.md, CLAUDE.md, .claude/settings.json) — item 2, coordinator
# task. `bootstrap_workspace` (not `copy_fixture`) is the setup here: these rules
# only ever fire once a digest is actually recorded (grison.validator.core
# ._check_scaffolded_files's own docstring — a fresh, never-scaffolded workspace,
# like every `copy_fixture` fixture, must never be flagged), so a REAL scaffold run
# is what puts each recorded digest in place.
# ---------------------------------------------------------------------------


@pytest.mark.rule_ok("WS-011", "WS-012")
def test_ws011_ws012_freshly_scaffolded_workspace_passes(tmp_path: Path) -> None:
    """A real positive proof: every recorded digest matches its file exactly right
    after `grison scaffold` (via bootstrap) runs — neither rule fires."""
    bootstrap_workspace(tmp_path)
    fails = rule_ids(validate_workspace(tmp_path))
    assert "WS-011" not in fails
    assert "WS-012" not in fails


@pytest.mark.rule("WS-011")
def test_ws011_spec_md_missing(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / ".grison" / "SPEC.md").unlink()
    fails = validate_workspace(tmp_path)
    assert "WS-011" in rule_ids(fails)


def test_ws011_template_missing(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / ".grison" / "templates" / "finding-library.md").unlink()
    fails = validate_workspace(tmp_path)
    assert "WS-011" in rule_ids(fails)


def test_ws011_claude_md_missing(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    fails = validate_workspace(tmp_path)
    assert "WS-011" in rule_ids(fails)


def test_ws011_settings_json_missing(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    (tmp_path / ".claude" / "settings.json").unlink()
    fails = validate_workspace(tmp_path)
    assert "WS-011" in rule_ids(fails)


@pytest.mark.rule("WS-012")
def test_ws012_spec_md_hand_edited(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    spec = tmp_path / ".grison" / "SPEC.md"
    spec.write_text(spec.read_text(encoding="utf-8") + "\nhand-added line\n", encoding="utf-8")
    fails = validate_workspace(tmp_path)
    assert "WS-012" in rule_ids(fails)


def test_ws012_template_content_drift_is_not_flagged(tmp_path: Path) -> None:
    """Design choice (item 2's report): a template is a copy-and-edit starting
    point — `grison.scaffold.orchestrate`'s own docstring says grison never touches
    one again once it exists, even with `--force` — so content drift on a template
    is the INTENDED use, not corruption; only its ABSENCE (WS-011, tested above) is
    ever checked for a template."""
    bootstrap_workspace(tmp_path)
    tmpl = tmp_path / ".grison" / "templates" / "finding-library.md"
    tmpl.write_text(tmpl.read_text(encoding="utf-8") + "\ncustomized by the team\n",
                    encoding="utf-8")
    fails = validate_workspace(tmp_path)
    assert "WS-012" not in rule_ids(fails)


def test_ws012_claude_md_addition_below_a_current_marker_is_not_flagged(tmp_path: Path) -> None:
    """The chosen rule (item 2's report): CLAUDE.md's digest match is gated on its
    OWN marker line first — a current marker (this exact grison would generate the
    same marker right now) is never flagged regardless of what's below it, so an
    operator's own added section survives untouched. Mirrors
    `grison.scaffold.orchestrate._scaffold_claude_md`'s own "up-to-date" tolerance
    exactly."""
    bootstrap_workspace(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        claude_md.read_text(encoding="utf-8") + "\n## Team notes\n\nOur own conventions.\n",
        encoding="utf-8",
    )
    fails = validate_workspace(tmp_path)
    assert "WS-012" not in rule_ids(fails)


def test_ws012_claude_md_stale_and_hand_edited_is_flagged(tmp_path: Path) -> None:
    """The other half of the same rule: once the marker line itself is no longer
    what this grison would generate (simulating an older-grison-generated copy) AND
    the body no longer matches what was last recorded, that's a genuine stale
    hand-edit — flagged, matching `_scaffold_claude_md`'s own "stale-hand-edited"
    branch (the one case it leaves alone with a warning instead of regenerating)."""
    bootstrap_workspace(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    lines[0] = "<!-- grison-generated CLAUDE.md — grison 0.0.1-fake, spec format 1 -->"
    lines.append("hand-edited after the fact")
    claude_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fails = validate_workspace(tmp_path)
    assert "WS-012" in rule_ids(fails)


def test_ws012_settings_json_canonical_rule_removed_is_flagged(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["permissions"]["deny"] = [d for d in data["permissions"]["deny"] if "env" not in d]
    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    fails = validate_workspace(tmp_path)
    assert "WS-012" in rule_ids(fails)


def test_ws012_settings_json_users_own_additions_are_not_flagged(tmp_path: Path) -> None:
    """The chosen rule (item 2's report): the digest covers only grison's own
    canonical deny rules + hook-entry presence, never the whole file — a user's own
    extra deny rule (or any other top-level key) never changes it."""
    bootstrap_workspace(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["permissions"]["deny"].append("Bash(rm -rf /)")
    data["permissions"]["allow"] = ["Bash(ls*)"]
    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    fails = validate_workspace(tmp_path)
    assert "WS-012" not in rule_ids(fails)


def test_ws011_and_ws012_pass_again_after_rescaffold(tmp_path: Path) -> None:
    """`grison scaffold` re-running must make the rule pass again (item 2's explicit
    ask): SPEC.md hand-edited -> WS-012 -> `grison scaffold` (force, so it actually
    regenerates) -> clean again."""
    from grison.scaffold import scaffold_workspace

    bootstrap_workspace(tmp_path)
    spec = tmp_path / ".grison" / "SPEC.md"
    spec.write_text(spec.read_text(encoding="utf-8") + "\nhand-added line\n", encoding="utf-8")
    assert "WS-012" in rule_ids(validate_workspace(tmp_path))

    scaffold_workspace(tmp_path, force=True)

    fails = rule_ids(validate_workspace(tmp_path))
    assert "WS-011" not in fails
    assert "WS-012" not in fails
