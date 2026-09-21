"""Every rule id in ``grison/validator/registry.py``, ``docs/workspace-format.md``,
and the test suite's ``@pytest.mark.rule(...)``/``@pytest.mark.rule_ok(...)`` markers
must be EXACTLY the same set, in every direction — a rule with no spec section, no
registry entry, no failing-case test, or no passing-case test is a real gap; a marker
or spec mention naming a rule id that doesn't exist is a typo. This is what the brief
calls "grison's own test suite fails if a rule has no validator test."

Marker extraction goes through :mod:`ast`, not a text regex: a rule id mentioned in a
``#`` comment or a docstring is not a test, so it must never count as one — that was
the bug (a rule id merely mentioned somewhere in a test file's TEXT counted as
"covered" even with no real ``@pytest.mark.rule(...)``/``rule_ok(...)`` decorator on
an actual test function). ``test_every_registry_rule_is_documented`` is similarly
strict: a rule id must have a real definition entry — a row of its own in the spec's
"Appendix: full rule table" — not just appear anywhere in the document's prose (that
gap is exactly how REP-003 went undocumented in the appendix: it was named in running
prose in §3.1 but had no table row).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from grison.validator.registry import RULES

_RULE_ID_RE = re.compile(r"\b(?:WS|FND|REP|WIKI|REF|TXT|IDX)-\d{3}\b")
_APPENDIX_HEADING = "## Appendix: full rule table"
_APPENDIX_ROW_RE = re.compile(r"^\| ([A-Z]+-\d+) \|", re.MULTILINE)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC_PATH = _REPO_ROOT / "docs" / "workspace-format.md"
_TESTS_DIR = Path(__file__).resolve().parent.parent


def _spec_ids(text: str) -> set[str]:
    """Every rule id mentioned ANYWHERE in ``text`` — deliberately broad, used only
    to catch a typo'd/unregistered id (see ``test_spec_never_mentions_an_...``);
    never used to decide whether a rule is properly DOCUMENTED (that needs a real
    definition entry — see :func:`_spec_appendix_ids`)."""
    return set(_RULE_ID_RE.findall(text))


def _spec_appendix_ids(text: str) -> set[str]:
    """Every rule id with its OWN row in the spec's appendix rule table — the
    spec's one canonical, per-rule definition entry (every other section either
    covers several rules under one heading, e.g. "## 2. Finding documents
    (`FND-…`)", or omits ids from its heading entirely). A rule named only in
    running prose, with no row here, does not count as documented."""
    _, _, appendix = text.partition(_APPENDIX_HEADING)
    return set(_APPENDIX_ROW_RE.findall(appendix))


def _marker_rule_ids(decorator: ast.expr, kind: str) -> list[str]:
    """The string-literal args of ``decorator`` IF it is exactly a
    ``pytest.mark.<kind>(...)`` call — never matched textually, so a rule id
    sitting in a comment or docstring near a real decorator can never be mistaken
    for one of its arguments."""
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == kind):
        return []
    mark = func.value
    if not (isinstance(mark, ast.Attribute) and mark.attr == "mark"):
        return []
    root = mark.value
    if not (isinstance(root, ast.Name) and root.id == "pytest"):
        return []
    return [
        a.value for a in decorator.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]


def _marker_ids(kind: str, *, tests_dir: Path = _TESTS_DIR) -> set[str]:
    """Every rule id passed to a real ``@pytest.mark.<kind>(...)`` decorator on a
    real ``test_*`` function, anywhere under ``tests_dir`` — parsed with
    :mod:`ast`, so a comment-only or docstring-only mention is structurally
    invisible to this, not just filtered out after the fact."""
    ids: set[str] = set()
    for path in sorted(tests_dir.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            for dec in node.decorator_list:
                ids.update(_marker_rule_ids(dec, kind))
    return ids


def test_every_registry_rule_is_documented() -> None:
    missing = sorted(set(RULES) - _spec_appendix_ids(_SPEC_PATH.read_text(encoding="utf-8")))
    assert not missing, (
        f"rule id(s) with no row of their own in docs/workspace-format.md's "
        f"'{_APPENDIX_HEADING}': {missing}"
    )


def test_spec_never_mentions_an_unregistered_rule_id() -> None:
    unknown = sorted(_spec_ids(_SPEC_PATH.read_text(encoding="utf-8")) - set(RULES))
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
    assert not missing, f"rule id(s) with no @pytest.mark.rule_ok(...) passing-case test: {missing}"


def test_retired_rules_are_never_used_by_a_test_marker() -> None:
    retired = {rid for rid, rule in RULES.items() if rule.retired}
    used = _marker_ids("rule") | _marker_ids("rule_ok")
    assert not (retired & used), f"a test marker references a RETIRED rule id: {retired & used}"


def test_no_marker_references_an_unknown_rule() -> None:
    marked = _marker_ids("rule") | _marker_ids("rule_ok")
    unknown = sorted(marked - set(RULES))
    assert not unknown, f"a test marker references unknown rule id(s): {unknown}"


# --- proof: neither extractor is fooled by a mention with no real definition -------


def test_ast_marker_extraction_ignores_comment_and_docstring_mentions(tmp_path: Path) -> None:
    """Regression proof for the bug this module's rewrite fixes: the old extractor
    was a text regex over the WHOLE file, so a rule id sitting in a ``#`` comment or
    a docstring — never inside a real ``@pytest.mark.rule(...)`` call — still counted
    as a covered rule. A throwaway fixture file (never added to the real ``tests/``
    tree) proves the ast-based extractor no longer does that, while a real decorator
    two lines below it is still picked up correctly."""
    fake = tmp_path / "test_fake_marker_fixture.py"
    fake.write_text(
        '"""A docstring merely mentioning @pytest.mark.rule("ZZZ-901") — not code."""\n'
        "\n"
        "import pytest\n"
        "\n"
        '# another mention in a comment: pytest.mark.rule("ZZZ-902")\n'
        '@pytest.mark.rule("ZZZ-903")\n'
        "def test_real_marker() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    ids = _marker_ids("rule", tests_dir=tmp_path)
    assert ids == {"ZZZ-903"}


def test_appendix_extraction_ignores_a_prose_only_mention() -> None:
    """Regression proof for ``test_every_registry_rule_is_documented``'s stricter
    check: the old test only asked whether a rule id appeared ANYWHERE in the spec,
    so a rule mentioned only in running prose — with no heading, no appendix-table
    row, no definition of its own — still counted as "documented". This is exactly
    how REP-003 went undocumented in the appendix table until this fix. Proven here
    with a throwaway spec text, never written to disk."""
    fake_spec = (
        "Some prose that merely refers to `WIKI-999` in passing, with no definition "
        "entry of its own anywhere in this document.\n\n"
        f"{_APPENDIX_HEADING}\n\n"
        "| id | summary |\n|---|---|\n| WS-001 | unrelated |\n"
    )
    assert "WIKI-999" in _spec_ids(fake_spec)  # the broad, typo-catching check still finds it
    assert "WIKI-999" not in _spec_appendix_ids(fake_spec)  # the strict "documented" one does not
