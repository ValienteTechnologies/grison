"""Failing-case tests for FND-001..FND-017 (finding documents). Passing cases are
proven by ``tests/test_validator_fixture.py``'s clean-fixture test."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_LIB = "findings/library/weak-tls-config.md"


@pytest.mark.rule("FND-001")
def test_fnd001_unknown_field(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "severity: medium", "severity: medium\nbogus: 1")
    assert "FND-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-002")
def test_fnd002_missing_field(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "finding_type: network\n", "")
    assert "FND-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-003")
def test_fnd003_bad_severity(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "severity: medium", "severity: apocalyptic")
    assert "FND-003" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-004")
def test_fnd004_bad_finding_type(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "finding_type: network", "finding_type: outer-space")
    assert "FND-004" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-005")
def test_fnd005_bad_cvss(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "CVSS:3.1/AV:A/AC:L/PR:L/UI:R/S:U/C:L/I:L/A:N", "not-a-vector")
    assert "FND-005" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-006")
def test_fnd006_unknown_cwe(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "CWE-326", "CWE-99999999")
    assert "FND-006" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-007")
def test_fnd007_bad_tags(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "  - tls\n  - crypto\n", "  - tls\n  - TLS\n")  # case-insensitive dup
    assert "FND-007" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-008")
def test_fnd008_affected_entities_on_library(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "tags:", "affected_entities: https://example.com\ntags:")
    assert "FND-008" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-009")
def test_fnd009_missing_section(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "## Impact\n\nTraffic may be susceptible to downgrade and cryptographic attacks.\n\n",
        "",
    )
    assert "FND-009" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-010")
def test_fnd010_unknown_section(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "## Impact", "## Consequences")
    assert "FND-010" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-011")
def test_fnd011_duplicate_section(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "## Mitigation", "## Description")
    assert "FND-011" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-012")
def test_fnd012_sections_out_of_order(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _LIB
    text = p.read_text(encoding="utf-8")
    text = text.replace(
        "## Description\n\nThe server negotiates deprecated TLS 1.0/1.1 and weak cipher "
        "suites.\n\n## Impact",
        "## Impact",
    ).replace(
        "## Mitigation",
        "## Description\n\nThe server negotiates deprecated TLS 1.0/1.1 and weak cipher "
        "suites.\n\n## Mitigation",
    )
    p.write_text(text, encoding="utf-8")
    assert "FND-012" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-013")
def test_fnd013_missing_title(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "# Weak TLS Configuration\n\n", "")
    assert "FND-013" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-014")
def test_fnd014_body_not_convertible(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "Traffic may be susceptible to downgrade and cryptographic attacks.",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
    )
    assert "FND-014" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-015")
def test_fnd015_severity_cvss_mismatch(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _LIB, "severity: medium", "severity: critical")
    assert "FND-015" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-016")
def test_fnd016_unexpected_content(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "# Weak TLS Configuration\n\n## Description",
        "# Weak TLS Configuration\n\nstray preface text\n\n## Description",
    )
    assert "FND-016" in rule_ids(validate_workspace(root))


@pytest.mark.rule("FND-017")
def test_fnd017_bad_frontmatter(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / _LIB
    p.write_text(p.read_text(encoding="utf-8").lstrip("-\n").replace("---\n", "", 1))
    assert "FND-017" in rule_ids(validate_workspace(root))
