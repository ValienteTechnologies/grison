"""``.grison/templates/`` (task item 2): every template, copied to its proper place in
a real workspace, validates clean.
"""

from __future__ import annotations

from pathlib import Path

from grison.scaffold import templates
from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture


def test_all_four_templates_are_generated() -> None:
    all_t = templates.all_templates()
    assert set(all_t) == {
        templates.FINDING_LIBRARY_NAME,
        templates.FINDING_REPORTED_NAME,
        templates.WIKI_PAGE_NAME,
        templates.PROJECT_NOTE_NAME,
    }
    for text in all_t.values():
        assert text and text.endswith("\n")


def test_finding_library_template_validates_clean(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    dest = root / "findings" / "library" / "scaffold-template.md"
    dest.write_text(templates.finding_library_template())
    assert validate_workspace(root, paths=[dest]) == []


def test_finding_reported_template_validates_clean(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    dest = root / "findings" / "reports" / "14-acme-corp" / "scaffold-template.md"
    dest.write_text(templates.finding_reported_template())
    assert validate_workspace(root, paths=[dest]) == []


def test_wiki_page_template_validates_clean(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    dest = root / "methodology" / "library" / "web-application-testing" / "scaffold-template.md"
    dest.write_text(templates.wiki_page_template())
    assert validate_workspace(root, paths=[dest]) == []


def test_project_note_template_validates_clean(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    dest = root / "findings" / "reports" / "14-acme-corp" / "notes" / "scaffold-template.md"
    dest.write_text(templates.project_note_template())
    assert validate_workspace(root, paths=[dest]) == []


def test_reported_template_frontmatter_uses_live_enum_values() -> None:
    from grison.model.enums import FindingType, Severity

    text = templates.finding_reported_template()
    for member in Severity:
        assert member.value in text
    for member in FindingType:
        assert member.value in text
