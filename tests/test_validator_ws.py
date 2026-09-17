"""Failing-case tests for WS-001..WS-010 (workspace layout, hygiene, format version).
Passing cases mostly live in ``test_validator_fixture.py``; the ones needing their own
setup (git hygiene, a recorded mirror digest) are here too."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from grison.hashing import digest_text
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
