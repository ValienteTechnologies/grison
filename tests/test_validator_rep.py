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
        "One critical and one high finding were identified, both related to input "
        "validation.",
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
