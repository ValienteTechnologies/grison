"""Unit tests for the shared IR-level helpers consolidated out of the individual
parsers in the parser-convergence step: severity text-parsing fallback
(grison.scanners.ir.severity), bare-CVSS3 vector prefixing
(grison.scanners.ir.cvss2), and CWE-id normalisation (grison.scanners.ir.cwe)."""

from __future__ import annotations

from grison.scanners.ir.cvss2 import ensure_cvss3_prefix
from grison.scanners.ir.cwe import normalize_cwe
from grison.scanners.ir.severity import Severity, severity_or, severity_or_info

# --- severity_or / severity_or_info ---------------------------------------------


def test_severity_or_info_parses_a_known_name() -> None:
    assert severity_or_info("high") == Severity.HIGH


def test_severity_or_info_aliases_moderate_to_medium() -> None:
    assert severity_or_info("Moderate") == Severity.MEDIUM


def test_severity_or_info_falls_back_to_info_on_garbage() -> None:
    assert severity_or_info("not-a-severity") == Severity.INFO


def test_severity_or_info_falls_back_to_info_on_blank() -> None:
    assert severity_or_info("") == Severity.INFO


def test_severity_or_uses_the_given_default() -> None:
    assert severity_or("not-a-severity", Severity.MEDIUM) == Severity.MEDIUM
    assert severity_or("high", Severity.MEDIUM) == Severity.HIGH


# --- ensure_cvss3_prefix ---------------------------------------------------------


def test_ensure_cvss3_prefix_adds_prefix_to_bare_vector() -> None:
    assert (
        ensure_cvss3_prefix("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
        == "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    )


def test_ensure_cvss3_prefix_leaves_already_prefixed_vector_untouched() -> None:
    vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert ensure_cvss3_prefix(vector) == vector


def test_ensure_cvss3_prefix_empty_stays_empty() -> None:
    assert ensure_cvss3_prefix("") == ""
    assert ensure_cvss3_prefix("   ") == ""


# --- normalize_cwe ---------------------------------------------------------------


def test_normalize_cwe_bare_digit_string() -> None:
    assert normalize_cwe("79") == "CWE-79"


def test_normalize_cwe_already_prefixed() -> None:
    assert normalize_cwe("CWE-79") == "CWE-79"


def test_normalize_cwe_int_input() -> None:
    assert normalize_cwe(79) == "CWE-79"


def test_normalize_cwe_zero_sentinel_yields_empty() -> None:
    assert normalize_cwe("0") == ""


def test_normalize_cwe_negative_one_sentinel_yields_empty() -> None:
    assert normalize_cwe("-1") == ""


def test_normalize_cwe_none_yields_empty() -> None:
    assert normalize_cwe(None) == ""


def test_normalize_cwe_blank_yields_empty() -> None:
    assert normalize_cwe("") == ""
    assert normalize_cwe("CWE-") == ""


def test_normalize_cwe_non_numeric_yields_empty() -> None:
    assert normalize_cwe("garbage") == ""
