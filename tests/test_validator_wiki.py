"""Failing-case tests for WIKI-001..WIKI-014 (wiki page documents)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_RECON = "methodology/library/web-application-testing/recon.md"


@pytest.mark.rule("WIKI-001")
def test_wiki001_unknown_field(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "priority: 1", "priority: 1\nbogus: 1")
    assert "WIKI-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-002")
def test_wiki002_blank_title(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "title: Reconnaissance overview", "title: ''")
    assert "WIKI-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-003")
def test_wiki003_bad_priority(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "priority: 1", "priority: soon")
    assert "WIKI-003" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-004")
def test_wiki004_bad_tags(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "tags:\n  - recon", "tags:\n  - recon\n  - RECON")
    assert "WIKI-004" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-005")
def test_wiki005_raw_html(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "# Tooling", "<div>real html</div>\n\n# Tooling")
    assert "WIKI-005" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-006")
def test_wiki006_bad_link_scheme(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _RECON,
        "https://example.com/blog/recon",
        "file:///etc/passwd",
    )
    assert "WIKI-006" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-007")
def test_wiki007_broken_internal_link(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _RECON,
        "/books/web-application-testing/chapter/reconnaissance",
        "/books/web-application-testing/chapter/does-not-exist",
    )
    assert "WIKI-007" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-008")
def test_wiki008_control_char(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "Start with passive", "Start​ with passive")
    assert "WIKI-008" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-009")
def test_wiki009_crlf(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _RECON
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert "WIKI-009" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-010")
def test_wiki010_trailing_whitespace(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "# Tooling\n", "# Tooling   \n")
    assert "WIKI-010" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-011")
def test_wiki011_bad_eof(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _RECON
    p.write_text(p.read_text(encoding="utf-8") + "\n")  # two trailing newlines
    assert "WIKI-011" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-012")
def test_wiki012_heading_skip(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "# Tooling", "### Tooling")
    assert "WIKI-012" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-013")
def test_wiki013_title_repeated(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _RECON, "# Tooling", "# Reconnaissance overview")
    assert "WIKI-013" in rule_ids(validate_workspace(root))


@pytest.mark.rule("WIKI-014")
def test_wiki014_bad_frontmatter(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _RECON
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("---\n", "", 1))
    assert "WIKI-014" in rule_ids(validate_workspace(root))
