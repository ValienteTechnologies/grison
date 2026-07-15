"""Tests for offline document validation."""

from __future__ import annotations

from pathlib import Path

from grison.validate import validate_file


def test_validate_file_reports_undecodable_bytes_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "not-utf8.md"
    path.write_bytes(b"\xff\xfe garbage, not a real utf-8 document")

    errors = validate_file(path)

    assert len(errors) == 1
    assert errors[0].startswith("cannot read:")
