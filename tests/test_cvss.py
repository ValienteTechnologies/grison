"""Tests for the CVSS 3.0/3.1 base vector parser and scorer."""

from __future__ import annotations

import pytest

from grison.model.cvss import CvssError, CvssVector, is_valid_cvss, parse_cvss


def test_known_31_vector_critical_score() -> None:
    vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    result = parse_cvss(vector)
    assert isinstance(result, CvssVector)
    assert result.raw == vector
    assert result.version == "3.1"
    assert result.base_score == 9.8
    assert result.metrics == {
        "AV": "N",
        "AC": "L",
        "PR": "N",
        "UI": "N",
        "S": "U",
        "C": "H",
        "I": "H",
        "A": "H",
    }


def test_known_31_vector_zero_score() -> None:
    vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:N/I:N/A:N"
    result = parse_cvss(vector)
    assert result.base_score == 0.0


def test_same_vector_as_30_is_accepted_and_scored() -> None:
    vector = "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    result = parse_cvss(vector)
    assert result.version == "3.0"
    assert result.base_score == 9.8


def test_cvss_20_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:2.0/AV:N/AC:L/Au:N/C:C/I:C/A:C")


def test_cvss_40_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N")


def test_missing_metric_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H")


def test_illegal_value_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:3.1/AV:Z/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")


def test_no_prefix_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")


def test_duplicate_metric_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:3.1/AV:N/AV:A/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")


def test_unknown_metric_key_rejected() -> None:
    with pytest.raises(CvssError):
        parse_cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/ZZ:X")


def test_is_valid_cvss_true() -> None:
    assert is_valid_cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") is True


def test_is_valid_cvss_false() -> None:
    assert is_valid_cvss("CVSS:2.0/AV:N/AC:L/Au:N/C:C/I:C/A:C") is False
    assert is_valid_cvss("not a vector") is False


def test_temporal_metrics_ignored_for_base_score() -> None:
    base = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    with_temporal = base + "/E:H/RL:O/RC:C"
    result = parse_cvss(with_temporal)
    assert result.base_score == 9.8
    assert result.metrics == parse_cvss(base).metrics
