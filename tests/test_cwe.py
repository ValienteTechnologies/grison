"""Tests for the embedded CWE index loader/validator (``grison.model.cwe``)."""

from __future__ import annotations

import pytest

from grison.model.cwe import cwe_name, is_known_cwe, normalize_cwe


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("16", "CWE-16"),
        ("CWE-16", "CWE-16"),
        ("cwe-16", "CWE-16"),
        (" CWE-0016 ", "CWE-16"),
        ("79", "CWE-79"),
    ],
)
def test_normalize_cwe_accepts_variants(raw: str, expected: str) -> None:
    assert normalize_cwe(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-cwe", "CWE-", "CWE-abc", "12CWE", None, 42])
def test_normalize_cwe_rejects_junk(raw) -> None:
    assert normalize_cwe(raw) is None


def test_cwe_name_by_canonical_id() -> None:
    assert cwe_name("CWE-16") == "Configuration"


def test_cwe_name_by_bare_number() -> None:
    assert cwe_name("16") == "Configuration"


def test_cwe_name_resolves_known_weakness() -> None:
    assert cwe_name("CWE-79") is not None
    assert "Cross-site Scripting" in cwe_name("CWE-79")


def test_cwe_name_unknown_returns_none_not_error() -> None:
    assert cwe_name("CWE-9999999") is None


def test_is_known_cwe_true_for_real_id() -> None:
    assert is_known_cwe("CWE-79") is True


def test_is_known_cwe_false_for_nonexistent_id() -> None:
    assert is_known_cwe("CWE-9999999") is False


def test_is_known_cwe_false_for_junk() -> None:
    assert is_known_cwe("not-a-cwe") is False
