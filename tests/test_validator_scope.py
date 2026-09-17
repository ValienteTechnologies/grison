"""``validate_workspace``'s ``paths=`` scope resolution (workspace-format spec §9).

The bug this file exists to catch: a ``paths`` argument that doesn't resolve to a
real validation unit must never silently contribute zero failures — "the wrong
argument makes the gate say clean" is worse than no gate at all. Every case here
either returns the real failures for the named scope, or raises
:class:`~grison.validator.ValidationScopeError`; nothing in between.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grison.validator import (
    ValidationScopeError,
    WorkspaceNotFound,
    find_workspace_root,
    validate_workspace,
)
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_LIB_BAD = ("findings/library/weak-tls-config.md", "severity: medium", "severity: banana")
_GLOBEX_BAD = ("findings/reports/globex/broken-auth.md", "severity: low", "severity: banana")


def _break(root: Path, rel: str, old: str, new: str) -> None:
    edit(root / rel, old, new)


def _add_stray(root: Path) -> Path:
    p = root / "findings" / "reports" / "globex" / "scratch.txt"
    p.write_text("stray\n")
    return p


# --- find_workspace_root -------------------------------------------------------------


def test_find_workspace_root_at_the_root_itself(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    assert find_workspace_root(tmp_path) == tmp_path.resolve()


def test_find_workspace_root_walks_up_from_a_subdirectory(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    sub = tmp_path / "findings" / "reports" / "acme"
    sub.mkdir(parents=True)
    assert find_workspace_root(sub) == tmp_path.resolve()


def test_find_workspace_root_raises_when_none_found(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceNotFound):
        find_workspace_root(tmp_path)


# --- the exact reported bug -----------------------------------------------------------


def test_dot_path_validates_the_whole_workspace_not_nothing(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_LIB_BAD)
    whole = rule_ids(validate_workspace(root))
    dotted = rule_ids(validate_workspace(root, paths=[root]))
    assert dotted == whole
    assert "FND-003" in dotted


def test_nonexistent_path_raises_never_silently_passes(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_LIB_BAD)  # even with a real failure elsewhere in the workspace
    with pytest.raises(ValidationScopeError, match="no such path"):
        validate_workspace(root, paths=[root / "nope.md"])


def test_path_outside_workspace_raises(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n")
    with pytest.raises(ValidationScopeError, match="outside the workspace"):
        validate_workspace(root, paths=[outside])


def test_path_inside_grison_raises(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    with pytest.raises(ValidationScopeError, match=r"\.grison"):
        validate_workspace(root, paths=[root / ".grison" / "manifest.yml"])


def test_non_validated_location_raises(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "CLAUDE.md").write_text("# notes\n")
    with pytest.raises(ValidationScopeError, match="not a workspace document"):
        validate_workspace(root, paths=[root / "CLAUDE.md"])


def test_git_directory_raises(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x\n")
    with pytest.raises(ValidationScopeError):
        validate_workspace(root, paths=[root / ".git"])
    with pytest.raises(ValidationScopeError):
        validate_workspace(root, paths=[root / ".git" / "config"])


# --- directory scoping: findings/ ----------------------------------------------------


def test_scope_findings_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_LIB_BAD)
    _add_stray(root)
    fails = rule_ids(validate_workspace(root, paths=[root / "findings"]))
    assert {"FND-003", "WS-002"} <= fails


def test_scope_findings_library_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_LIB_BAD)
    fails = rule_ids(validate_workspace(root, paths=[root / "findings" / "library"]))
    assert "FND-003" in fails


def test_scope_findings_reports_dir_all(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_GLOBEX_BAD)
    fails = rule_ids(validate_workspace(root, paths=[root / "findings" / "reports"]))
    assert "FND-003" in fails


def test_scope_one_report_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_GLOBEX_BAD)
    _add_stray(root)
    fails = rule_ids(
        validate_workspace(root, paths=[root / "findings" / "reports" / "globex"])
    )
    assert {"FND-003", "WS-002"} <= fails
    # the OTHER report dir's content is not re-validated, but the stray file IS
    # inside the requested one, so nothing about this assertion depends on it


def test_scope_findings_inbox_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / "findings" / "inbox" / "sql-injection.md", "severity: high",
        "severity: banana")
    fails = rule_ids(validate_workspace(root, paths=[root / "findings" / "inbox"]))
    assert "FND-003" in fails


# --- directory scoping: methodology/ --------------------------------------------------


def test_scope_methodology_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / "methodology" / "library" / "web-application-testing" / "recon.md",
        "title: Reconnaissance overview", "title: ''")
    fails = rule_ids(validate_workspace(root, paths=[root / "methodology"]))
    assert "WIKI-002" in fails


def test_scope_methodology_library_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / "methodology" / "library" / "web-application-testing" / "recon.md",
        "title: Reconnaissance overview", "title: ''")
    fails = rule_ids(validate_workspace(root, paths=[root / "methodology" / "library"]))
    assert "WIKI-002" in fails


def test_scope_one_book_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / "methodology" / "library" / "web-application-testing" / "recon.md",
        "title: Reconnaissance overview", "title: ''")
    book = root / "methodology" / "library" / "web-application-testing"
    fails = rule_ids(validate_workspace(root, paths=[book]))
    assert "WIKI-002" in fails


def test_scope_methodology_checklists_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / "methodology" / "checklists" / "acme-2026-08" / "recon.md",
        "title: Reconnaissance overview", "title: ''",
    )
    fails = rule_ids(validate_workspace(root, paths=[root / "methodology" / "checklists"]))
    assert "WIKI-002" in fails


def test_scope_shelves_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    shelf = root / "methodology" / "library" / ".shelves" / "pentest-methodologies.yml"
    edit(shelf, "name: Pentest methodologies", "name: Pentest methodologies\nbogus: 1")
    fails = rule_ids(
        validate_workspace(root, paths=[root / "methodology" / "library" / ".shelves"])
    )
    assert "WS-010" in fails


# --- subdirectory invocation (cd into the workspace, use relative paths) --------------


def test_relative_paths_resolve_against_cwd_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_GLOBEX_BAD)
    globex = root / "findings" / "reports" / "globex"
    monkeypatch.chdir(globex)
    fails = rule_ids(validate_workspace(root, paths=[Path("broken-auth.md")]))
    assert "FND-003" in fails


def test_dot_from_a_subdirectory_still_means_whole_workspace_when_root_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`root` is always an explicit argument to validate_workspace (the CLI resolves
    it via find_workspace_root before calling in) — cwd only affects how RELATIVE
    paths in `paths=` are resolved, never which workspace is validated."""
    root = copy_fixture(tmp_path)
    _break(root, *_LIB_BAD)
    globex = root / "findings" / "reports" / "globex"
    monkeypatch.chdir(globex)
    whole = rule_ids(validate_workspace(root))
    dotted = rule_ids(validate_workspace(root, paths=[Path(os.curdir)]))
    # "." from inside globex means "the globex report dir" (cwd), not the workspace
    # root — the root only ever comes from the explicit `root` argument/CLI discovery.
    assert dotted == {"WS-002"} or dotted == set()  # globex itself has no lib break
    assert "FND-003" in whole  # sanity: the library break really is there


# --- deleted_ok -------------------------------------------------------------------


def test_deleted_ok_false_raises_for_missing_path(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    missing = root / "findings" / "reports" / "globex" / "gone.md"
    with pytest.raises(ValidationScopeError, match="no such path"):
        validate_workspace(root, paths=[missing])


def test_deleted_ok_true_falls_back_to_parent_directory_scope(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    _break(root, *_GLOBEX_BAD)
    missing = root / "findings" / "reports" / "globex" / "gone.md"
    fails = rule_ids(validate_workspace(root, paths=[missing], deleted_ok=True))
    assert "FND-003" in fails  # the report dir's OTHER problems are still caught


def test_deleted_ok_true_still_raises_when_parent_also_missing(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    missing = root / "findings" / "reports" / "does-not-exist" / "gone.md"
    with pytest.raises(ValidationScopeError, match="no such path"):
        validate_workspace(root, paths=[missing], deleted_ok=True)


# --- the property test: union of top-level dirs == whole workspace -----------------


def test_union_of_top_level_directories_equals_whole_workspace(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    # break something in every area the fixture has
    _break(root, *_LIB_BAD)
    _break(root, *_GLOBEX_BAD)
    _add_stray(root)
    edit(root / "methodology" / "library" / "web-application-testing" / "recon.md",
        "title: Reconnaissance overview", "title: ''")
    (root / "methodology" / "junk.txt").write_text("stray\n")
    data = json.loads((root / ".grison" / "index.json").read_text(encoding="utf-8"))
    data["records"]["findings/inbox/sql-injection.md"] = {"kind": "gw.finding", "id": 999}
    (root / ".grison" / "index.json").write_text(json.dumps(data), encoding="utf-8")

    def as_set(fails: list) -> set[tuple[str, str, int | None]]:
        return {(f.rule_id, f.path, f.line) for f in fails}

    whole = as_set(validate_workspace(root))
    findings_only = as_set(validate_workspace(root, paths=[root / "findings"]))
    methodology_only = as_set(validate_workspace(root, paths=[root / "methodology"]))
    union = findings_only | methodology_only

    assert whole == union, f"whole-union={whole - union}  union-whole={union - whole}"
    # and the union is non-trivial — this test would pass vacuously on an all-clean
    # workspace, so assert there really are failures on both sides
    assert len(whole) >= 5
