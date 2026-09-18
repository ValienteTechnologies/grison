"""End-to-end: a first bootstrap in an empty directory yields a complete, valid,
self-contained workspace (task item 9).
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from grison import manifest as manifest_mod
from grison.remote.bootstrap import bootstrap_workspace
from grison.validator import validate_workspace


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_bootstrap_into_empty_dir_validates_clean(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    failures = validate_workspace(tmp_path)
    assert failures == [], "\n".join(f"{f.rule_id} {f.path}: {f.message}" for f in failures)


def test_digests_recorded_for_spec_templates_and_claude_md(tmp_path: Path) -> None:
    from grison.hashing import digest_text
    from grison.scaffold.claude_md import build_claude_md
    from grison.scaffold.spec import spec_text
    from grison.scaffold.templates import all_templates
    from grison.validator.mirrors import expected_digest

    bootstrap_workspace(tmp_path)

    assert expected_digest(tmp_path, ".grison/SPEC.md") == digest_text(spec_text())
    assert expected_digest(tmp_path, "CLAUDE.md") == digest_text(build_claude_md())
    for name, content in all_templates().items():
        rel = f".grison/templates/{name}"
        assert expected_digest(tmp_path, rel) == digest_text(content), rel


def test_every_scaffolded_file_is_present(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    for rel in (
        "CLAUDE.md",
        ".claude/settings.json",
        ".gitignore",
        ".grison/SPEC.md",
        ".grison/templates/finding-library.md",
        ".grison/templates/finding-reported.md",
        ".grison/templates/wiki-page.md",
        ".grison/templates/project-note.md",
        ".grison/terms.txt",
        ".grison/manifest.yml",
        ".grison/index.json",
    ):
        assert (tmp_path / rel).exists(), f"missing {rel}"


def test_private_files_are_0600_and_dirs_0700(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    assert stat.S_IMODE((tmp_path / ".grison" / "terms.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".grison" / "env").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".grison").stat().st_mode) == 0o700


def test_git_tracking_class_via_check_git_hygiene(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    bootstrap_workspace(tmp_path)
    problems = manifest_mod.check_git_hygiene(tmp_path)
    assert problems == [], problems


def test_git_tracking_class_directly(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    bootstrap_workspace(tmp_path)

    def ignored(rel: str) -> bool:
        r = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--quiet", rel],
            capture_output=True,
        )
        return r.returncode == 0

    # private, must be ignored
    for rel in (".grison/env", ".grison/state", ".grison/snapshots", ".grison/terms.txt"):
        assert ignored(rel), f"{rel} should be git-ignored"
    # tracked, must NOT be ignored
    for rel in (
        ".grison/manifest.yml", ".grison/index.json", ".grison/SPEC.md",
        ".grison/templates", ".grison/templates/finding-library.md",
        "CLAUDE.md", ".claude/settings.json", ".gitignore",
    ):
        assert not ignored(rel), f"{rel} should NOT be git-ignored"


def test_precommit_hook_installed_when_a_git_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    bootstrap_workspace(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert hook.stat().st_mode & 0o111


def test_no_precommit_hook_when_not_a_git_repo(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    assert not (tmp_path / ".git").exists()


def test_bootstrap_is_idempotent_end_to_end(tmp_path: Path) -> None:
    first = bootstrap_workspace(tmp_path)
    claude_before = (tmp_path / "CLAUDE.md").read_text()
    second = bootstrap_workspace(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == claude_before
    assert first.scaffold.claude_md_status == "created"
    assert second.scaffold.claude_md_status == "up-to-date"
    assert validate_workspace(tmp_path) == []


@pytest.mark.parametrize("disabled_var", ["GRISON_CLAUDE_MD"])
def test_claude_md_off_setting_still_scaffolds_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled_var: str
) -> None:
    monkeypatch.setenv(disabled_var, "off")
    bootstrap_workspace(tmp_path)
    assert not (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".grison" / "SPEC.md").exists()
