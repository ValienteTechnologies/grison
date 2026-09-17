"""Failing-case tests for REF-001..REF-007 (D1 evidence / D9 wiki-image references)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_XSS = "findings/reports/14-acme-corp/reflected-xss.md"
_LIB = "findings/library/weak-tls-config.md"
_RECON = "methodology/library/web-application-testing/recon.md"
_SUBDOMAIN = "methodology/library/web-application-testing/reconnaissance/subdomain-enum.md"


@pytest.mark.rule("REF-001")
def test_ref001_image_not_alone(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _XSS,
        '![Alert firing in the browser](evidence/xss-alert.png "captured during testing")',
        'See ![Alert firing in the browser](evidence/xss-alert.png "captured during testing") '
        "above.",
    )
    assert "REF-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-002")
def test_ref002_unresolved_image(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _XSS, "evidence/xss-alert.png \"captured", "evidence/missing.png \"captured")
    assert "REF-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-003")
def test_ref003_stem_collision(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "xss-alert.txt").write_text("same stem as xss-alert.png\n")
    assert "REF-003" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-004")
def test_ref004_caption_conflict(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    narrative = (
        root / "findings" / "reports" / "14-acme-corp" / "narrative" / "executive_summary.md"
    )
    edit(
        narrative,
        "One critical and one high finding",
        '![A completely different caption](evidence/xss-alert.png)\n\n'
        "One critical and one high finding",
    )
    fails = validate_workspace(root)
    assert "REF-004" in rule_ids(fails)


@pytest.mark.rule("REF-005")
def test_ref005_image_in_library(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "Traffic may be susceptible to downgrade and cryptographic attacks.",
        "Traffic may be susceptible.\n\n![Not allowed](evidence/anything.png)",
    )
    assert "REF-005" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-006")
def test_ref006_bad_cross_reference(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _XSS,
        "[the alert screenshot](evidence/xss-alert.png)",
        "[the alert screenshot](evidence/missing.png)",
    )
    assert "REF-006" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-007")
def test_ref007_wrong_wiki_image_spelling(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _SUBDOMAIN,
        "../images/recon-diagram.png",
        "images/recon-diagram.png",
    )
    assert "REF-007" in rule_ids(validate_workspace(root))
