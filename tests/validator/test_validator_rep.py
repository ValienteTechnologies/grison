"""Failing-case tests for REP-001/REP-002 (narrative sections + notes)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_NARRATIVE = "findings/reports/14-acme-corp/narrative/executive_summary.md"
_NEW_NOTE = "findings/reports/14-acme-corp/notes/follow-up.md"
_MIRRORED_NOTE = "findings/reports/14-acme-corp/notes/8-client-note.md"


@pytest.mark.rule("REP-001")
def test_rep001_narrative_body_not_convertible(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _NARRATIVE,
        "One critical and one high finding were identified, both related to input validation.",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
    )
    assert "REP-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REP-001")
def test_rep001_new_note_body_not_convertible(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / _NEW_NOTE).write_text("<div>raw html not allowed</div>\n")
    assert "REP-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REP-002")
def test_rep002_indexed_note_missing_frontmatter(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _MIRRORED_NOTE
    edit(p, "---\nauthor: Jordan Lee\ntimestamp: '2026-08-05'\n---\n\n", "")
    assert "REP-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REP-002")
def test_rep002_unindexed_note_has_frontmatter(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _NEW_NOTE
    p.write_text("---\nauthor: Someone\n---\n\n" + p.read_text(encoding="utf-8"))
    assert "REP-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REP-003")
def test_rep003_unknown_narrative_field(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    # 14-acme-corp's .report.yml records narrative_order: [executive_summary] —
    # a field not in that list is REP-003, offline, with no Ghostwriter contact.
    unknown = root / "findings/reports/14-acme-corp/narrative/not_a_real_field.md"
    unknown.write_text("Some narrative text.\n")
    assert "REP-003" in rule_ids(validate_workspace(root))


@pytest.mark.rule_ok("REP-003")
def test_rep003_ok_when_no_report_yml_recorded_yet(tmp_path: Path) -> None:
    """No .report.yml (or one with an empty narrative_order) means nothing recorded
    to compare against yet — REP-003 never fires, same convention as WS-009's digest
    check for a mirror that has never been generated."""
    root = copy_fixture(tmp_path)
    (root / "findings/reports/globex/.report.yml").unlink()
    assert "REP-003" not in rule_ids(validate_workspace(root))
