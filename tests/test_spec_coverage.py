"""Every rule id in ``grison/validator/registry.py``, ``docs/workspace-format.md``,
and the test suite's ``@pytest.mark.rule(...)``/``@pytest.mark.rule_ok(...)`` markers
must be EXACTLY the same set, in every direction — a rule with no spec section, no
registry entry, no failing-case test, or no passing-case test is a real gap; a marker
or spec mention naming a rule id that doesn't exist is a typo. This is what the brief
calls "grison's own test suite fails if a rule has no validator test."
"""

from __future__ import annotations

import re
from pathlib import Path

from grison.validator.registry import RULES

_RULE_ID_RE = re.compile(r"\b(?:WS|FND|REP|WIKI|REF|TXT|IDX)-\d{3}\b")
# A marker call's ARGUMENT LIST, not just its first argument — `rule_ok(...)` in
# particular is written with many rule ids in one call (the "fixture validates clean"
# test proves many rules' passing case at once), often wrapped across several lines,
# so the call is matched non-greedily up to its closing paren and every quoted rule id
# inside that span is taken, not just one.
_CALL_RE = re.compile(r"pytest\.mark\.(rule|rule_ok)\((.*?)\)", re.DOTALL)
_ARG_RE = re.compile(r'[\'"]([A-Z]+-\d+)[\'"]')

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _REPO_ROOT / "docs" / "workspace-format.md"
_TESTS_DIR = Path(__file__).resolve().parent


def _spec_ids() -> set[str]:
    return set(_RULE_ID_RE.findall(_SPEC_PATH.read_text(encoding="utf-8")))


def _marker_ids(kind: str) -> set[str]:
    ids: set[str] = set()
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for name, args in _CALL_RE.findall(text):
            if name == kind:
                ids.update(_ARG_RE.findall(args))
    return ids


def test_every_registry_rule_is_documented() -> None:
    missing = sorted(set(RULES) - _spec_ids())
    assert not missing, f"rule id(s) missing from docs/workspace-format.md: {missing}"


def test_spec_never_mentions_an_unregistered_rule_id() -> None:
    unknown = sorted(_spec_ids() - set(RULES))
    assert not unknown, f"docs/workspace-format.md mentions unregistered rule id(s): {unknown}"


def _active_rule_ids() -> set[str]:
    """Every rule id except retired ones — a retired rule (see ``Rule.retired`` /
    ``registry._retire``) keeps its id reserved and documented, but nothing calls
    :func:`grison.validator.registry.fail` with it any more, so there is nothing left
    to write a failing/passing-case test for."""
    return {rid for rid, rule in RULES.items() if not rule.retired}


def test_every_rule_has_a_failing_case_test() -> None:
    marked = _marker_ids("rule")
    missing = sorted(_active_rule_ids() - marked)
    assert not missing, f"rule id(s) with no @pytest.mark.rule(...) failing-case test: {missing}"


def test_every_rule_has_a_passing_case_test() -> None:
    marked = _marker_ids("rule_ok")
    missing = sorted(_active_rule_ids() - marked)
    assert not missing, (
        f"rule id(s) with no @pytest.mark.rule_ok(...) passing-case test: {missing}"
    )


def test_retired_rules_are_never_used_by_a_test_marker() -> None:
    retired = {rid for rid, rule in RULES.items() if rule.retired}
    used = _marker_ids("rule") | _marker_ids("rule_ok")
    assert not (retired & used), f"a test marker references a RETIRED rule id: {retired & used}"


def test_no_marker_references_an_unknown_rule() -> None:
    marked = _marker_ids("rule") | _marker_ids("rule_ok")
    unknown = sorted(marked - set(RULES))
    assert not unknown, f"a test marker references unknown rule id(s): {unknown}"
