"""Failing-case tests for IDX-001..IDX-004 (index consistency)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, rule_ids

_INDEX = ".grison/index.json"


def _load(root: Path) -> dict:
    return json.loads((root / _INDEX).read_text(encoding="utf-8"))


def _save(root: Path, data: dict) -> None:
    (root / _INDEX).write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.rule("IDX-001")
def test_idx001_malformed_index(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / _INDEX).write_text("{not valid json", encoding="utf-8")
    assert "IDX-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("IDX-002")
def test_idx002_kind_path_mismatch(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    data = _load(root)
    data["records"]["findings/library/weak-tls-config.md"]["kind"] = "bs.page"
    _save(root, data)
    assert "IDX-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("IDX-003")
def test_idx003_unindexed_report_dir(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    data = _load(root)
    del data["records"]["findings/reports/14-acme-corp"]
    _save(root, data)
    fails = validate_workspace(root)
    assert "IDX-003" in rule_ids(fails)


@pytest.mark.rule("IDX-004")
def test_idx004_evidence_kind_mismatch(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    data = _load(root)
    data["records"]["findings/reports/14-acme-corp/evidence/xss-alert.png"]["kind"] = (
        "gw.reportedFinding"
    )
    _save(root, data)
    assert "IDX-004" in rule_ids(validate_workspace(root))
