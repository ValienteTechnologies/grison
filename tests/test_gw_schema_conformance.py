"""Permanent CI check: every GraphQL operation grison sends must validate against the
real Ghostwriter 7.2.6 schema (``tests/fixtures/gw-schema-7.2.6.graphql``, introspected
from a real instance — see ``lab/LAB.md`` in the rework repo).

This is the check that would have caught the 7.2 evidence-query break before it
shipped: two operations are known, confirmed-live failures today
(``_EVIDENCE_QUERY`` selects a field ``evidence`` does not have; ``_UPLOAD_EVIDENCE_
MUTATION`` sends an argument ``uploadEvidence`` does not accept and omits the
required ``report`` argument) — see ``tests/fakes/gw_server.py``'s module docstring
and ``tests/e2e/test_findings_e2e.py`` for the end-to-end proof. Every other
module-level query/mutation constant in ``grison.remote.ghostwriter`` must validate
clean. When the two known-broken operations get fixed, this test goes red on the
``assert not extra`` line below — that failure is the signal to shrink
``_KNOWN_FAILURES`` to ``{}``, not to touch this test's logic.
"""

from __future__ import annotations

from graphql import parse, validate

import grison.remote.ghostwriter as gw_module
from tests.fakes.gw_server import load_schema

# name -> exact set of graphql-core validation messages, captured by running this
# exact check against tests/fixtures/gw-schema-7.2.6.graphql (see the module
# docstring for the confirmed-live equivalents from the real lab server).
_KNOWN_FAILURES: dict[str, set[str]] = {
    "_EVIDENCE_QUERY": {
        "Cannot query field 'findingId' on type 'evidence'.",
    },
    "_UPLOAD_EVIDENCE_MUTATION": {
        "Unknown argument 'finding' on field 'mutation_root.uploadEvidence'.",
        "Field 'uploadEvidence' argument 'report' of type 'Int!' is required, but it "
        "was not provided.",
    },
}


def _module_level_operations() -> dict[str, str]:
    """Every module-level GraphQL operation string in ``grison.remote.ghostwriter`` —
    a private constant (leading ``_``) whose value is a query/mutation document."""
    return {
        name: value
        for name, value in vars(gw_module).items()
        if name.startswith("_")
        and name.endswith(("_QUERY", "_MUTATION"))
        and isinstance(value, str)
    }


def test_every_ghostwriter_operation_validates_against_the_real_schema_except_known_breaks():
    schema = load_schema()
    operations = _module_level_operations()
    assert operations, "no module-level *_QUERY/*_MUTATION constants found — extraction broke"

    failures: dict[str, set[str]] = {}
    for name, source in operations.items():
        errors = validate(schema, parse(source))
        if errors:
            failures[name] = {e.message for e in errors}

    extra = set(failures) - set(_KNOWN_FAILURES)
    assert not extra, (
        f"newly-broken Ghostwriter operation(s) against the 7.2.6 schema: "
        f"{ {n: failures[n] for n in extra} }"
    )

    fixed = set(_KNOWN_FAILURES) - set(failures)
    assert not fixed, (
        f"{fixed} now validate clean against the 7.2.6 schema — shrink _KNOWN_FAILURES "
        "in this test (and drop the matching xfail markers in tests/e2e/test_findings_e2e.py)"
    )

    for name, expected in _KNOWN_FAILURES.items():
        assert failures[name] == expected, (
            f"{name}'s validation errors changed shape — was {expected}, now {failures[name]}"
        )
