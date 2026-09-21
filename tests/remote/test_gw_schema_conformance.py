"""Permanent CI check: every GraphQL operation grison sends must validate against the
real Ghostwriter 7.2.6 schema (``tests/fixtures/gw-schema-7.2.6.graphql``, introspected
from a real instance — see ``lab/LAB.md`` in the rework repo).

This is the check that WOULD have caught the 7.2 evidence-query break before it
shipped: ``_EVIDENCE_QUERY`` used to select a ``findingId`` field ``evidence``
doesn't have, and ``_UPLOAD_EVIDENCE_MUTATION`` used to send a ``finding``
argument ``uploadEvidence`` doesn't accept while omitting the required ``report``
argument (D1: evidence belongs to a report, never a finding) — both fixed as part
of the engine-findings step (see ``grison.remote.gw_findings``/``gw_evidence``).
Every module-level query/mutation constant in ``grison.remote.ghostwriter`` must
now validate clean, no exceptions: this test is a permanent trip-wire, not a
one-time proof — a future change to any operation that drifts from the real
schema fails here immediately, by name, instead of surfacing as a confusing
mid-sync GraphQL error on whichever record happens to hit it first. See also
``grison.remote.compat.check_ghostwriter_compatibility``, which runs the exact
same check against the LIVE server at the start of every real sync.
"""

from __future__ import annotations

from graphql import parse, validate

import grison.remote.ghostwriter as gw_module
from tests.fakes.gw_server import load_schema


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


def test_every_ghostwriter_operation_validates_against_the_real_schema():
    schema = load_schema()
    operations = _module_level_operations()
    assert operations, "no module-level *_QUERY/*_MUTATION constants found — extraction broke"

    failures: dict[str, set[str]] = {}
    for name, source in operations.items():
        errors = validate(schema, parse(source))
        if errors:
            failures[name] = {e.message for e in errors}

    assert not failures, (
        f"Ghostwriter operation(s) that do not validate against the 7.2.6 schema: {failures}"
    )
