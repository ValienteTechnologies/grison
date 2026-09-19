"""``translate_validation_error`` — schema failures speak grison's own words, not
pydantic's internal phrasing, and carry a source line for frontmatter keys where the
YAML parser can give one."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.formats import finding as F
from grison.formats import mirrors as M
from grison.formats import wiki as W
from grison.formats.common import FormatError

_FINDING = """---
severity: high
finding_type: web
---
# T

## Description

d

## Impact

i

## Mitigation

m

## Replication Steps

r

## References

ref
"""


def _parse_finding(text: str) -> FormatError:
    with pytest.raises(FormatError) as ei:
        F.parse(text, path=Path("findings/library/x.md"))
    return ei.value


def test_enum_error_names_the_value_and_the_allowed_set() -> None:
    e = _parse_finding(_FINDING.replace("severity: high", "severity: banana"))
    assert e.detail == "severity is 'banana'; allowed: informational, low, medium, high, critical"
    assert "Input should be" not in e.detail  # not pydantic's own phrasing
    assert e.line == 2


def test_missing_field_error_names_the_field() -> None:
    e = _parse_finding(_FINDING.replace("finding_type: web\n", ""))
    assert e.detail == "finding_type is required"


def test_extra_field_error_names_the_field() -> None:
    e = _parse_finding(_FINDING.replace("finding_type: web", "finding_type: web\nbogus: 1"))
    assert e.detail == "bogus is not a recognized field"
    assert e.line == 4


def test_value_error_from_a_custom_field_validator_keeps_grisons_own_words() -> None:
    e = _parse_finding(
        _FINDING.replace("finding_type: web", "finding_type: web\ncwe:\n  - CWE-99999999")
    )
    assert "unknown CWE" in e.detail
    assert "Value error" not in e.detail
    assert e.line == 4


def test_duplicate_tag_error_has_a_line_number() -> None:
    e = _parse_finding(
        _FINDING.replace("finding_type: web", "finding_type: web\ntags:\n  - a\n  - A")
    )
    assert "duplicate tag" in e.detail
    assert e.line == 4


def test_type_error_describes_what_grison_expects() -> None:
    text = "---\ntitle: X\npriority: notanumber\n---\nbody\n"
    with pytest.raises(FormatError) as ei:
        W.parse(text, path=Path("methodology/library/b/p.md"))
    assert ei.value.detail == "priority is 'notanumber'; expected an integer"
    assert ei.value.line == 3


def test_nested_field_path_is_dotted_and_line_located() -> None:
    text = "title: Foo\nproject:\n  id: 1\n  client:\n    id: 2\nstatus:\n  complete: notabool\n"
    with pytest.raises(FormatError) as ei:
        M.parse_report_meta(text, path=Path("x"))
    assert ei.value.detail == "status.complete is 'notabool'; expected true or false"
    assert ei.value.line == 7


def test_no_line_when_field_is_missing_entirely() -> None:
    e = _parse_finding(_FINDING.replace("finding_type: web\n", ""))
    assert e.line is None


def test_root_level_model_error_has_grisons_own_message_no_line() -> None:
    e = _parse_finding(
        _FINDING.replace("finding_type: web", "finding_type: web\naffected_entities: foo")
    )
    assert e.detail == "affected_entities is instance-only; not allowed on a library finding"
    assert "Value error" not in e.detail
