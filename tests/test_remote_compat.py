"""Proofs for grison.remote.compat (BRIEF task F / ENGINE.md server compatibility
check): a warm path that costs one request when the cached fingerprint still
matches, and a cold path — full introspection + validate every operation — that
only runs when it doesn't."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import grison.remote.ghostwriter as gw_module
from grison.remote.compat import (
    SchemaCompatibilityError,
    check_ghostwriter_compatibility,
    fingerprint_from_schema,
    fingerprint_of,
    load_cache,
    save_cache,
)
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient
from tests.fakes.gw_server import FakeGhostwriter
from tests.fakes.gw_server import load_schema as load_fake_gw_schema


@pytest.fixture
def gw() -> FakeGhostwriter:
    return FakeGhostwriter()


@pytest.fixture
def gw_client(gw: FakeGhostwriter):
    creds = Creds(gw_url="https://fake", gw_token="test-gw-token")
    client = GhostwriterClient(creds, transport=gw.transport, sleep=lambda _: None)
    yield client
    client.close()


def test_cold_path_passes_clean_against_a_compatible_schema(
    gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """No cache present -> cold path -> proves the real introspection round-trip
    works end to end and grison's own operations validate clean today, and that
    it then caches the fingerprint (so a second call is warm)."""
    assert load_cache(tmp_path) is None
    check_ghostwriter_compatibility(gw_client, tmp_path)  # must not raise
    cached = load_cache(tmp_path)
    assert cached is not None and cached.fingerprint


def test_names_the_first_offending_field_and_the_min_version(
    gw_client: GhostwriterClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gw_module, "_FINDING_SEVERITY_QUERY", "query { findingSeverity { id bogusField } }",
    )
    with pytest.raises(SchemaCompatibilityError) as exc_info:
        check_ghostwriter_compatibility(gw_client, tmp_path)
    msg = str(exc_info.value)
    assert "bogusField" in msg
    assert "_FINDING_SEVERITY_QUERY" in msg
    assert "Ghostwriter >= 7.2.0" in msg
    # a rejected cold check must not cache — the next sync re-checks, never trusts
    # a schema it just rejected
    assert load_cache(tmp_path) is None


def test_reproduces_the_historical_evidence_query_break(
    gw_client: GhostwriterClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape as the real, confirmed-live 7.2 production break this check
    exists to catch (see tests/test_gw_schema_conformance.py's docstring)."""
    monkeypatch.setattr(
        gw_module, "_EVIDENCE_QUERY",
        "query { evidence { id findingId reportId document caption friendlyName description } }",
    )
    with pytest.raises(SchemaCompatibilityError, match="findingId"):
        check_ghostwriter_compatibility(gw_client, tmp_path)


def test_only_ever_reports_the_first_failure_deterministically(
    gw_client: GhostwriterClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operations are checked in sorted order — a caller can rely on the SAME
    operation being named first across runs when more than one is broken."""
    monkeypatch.setattr(gw_module, "_FINDING_SEVERITY_QUERY", "query { findingSeverity { x } }")
    monkeypatch.setattr(gw_module, "_FINDING_TYPE_LOOKUP_QUERY", "query { findingType { y } }")
    with pytest.raises(SchemaCompatibilityError, match="_FINDING_SEVERITY_QUERY"):
        check_ghostwriter_compatibility(gw_client, tmp_path)


def test_warm_path_issues_exactly_one_request(
    gw: FakeGhostwriter, gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """A cache whose fingerprint already matches the live server costs exactly
    the one fingerprint-probe request — no introspection, no per-operation
    validate. This is the fix for the ~30x per-sync slowdown the full
    introspection-every-sync design caused."""
    save_cache(tmp_path, fingerprint_from_schema(load_fake_gw_schema()))
    before = len(gw.request_log)
    check_ghostwriter_compatibility(gw_client, tmp_path)
    assert len(gw.request_log) - before == 1
    assert gw.request_log[-1].name == "__type"  # the probe, never "__schema" (full introspection)


def test_cold_path_runs_when_the_fingerprint_is_stale(
    gw: FakeGhostwriter, gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """A cached fingerprint that no longer matches the live server (the schema
    changed) forces the full introspection + validate pass, and then re-caches
    the new fingerprint."""
    save_cache(tmp_path, "sha256:stale-does-not-match-anything")
    before = len(gw.request_log)
    check_ghostwriter_compatibility(gw_client, tmp_path)
    # probe request + the full introspection request (at least 2; the cold path
    # is strictly more expensive than the warm one)
    assert len(gw.request_log) - before >= 2
    assert gw.request_log[before].name == "__type"
    assert any(op.name == "__schema" for op in gw.request_log[before:])
    fresh = load_cache(tmp_path)
    assert fresh is not None and fresh.fingerprint == fingerprint_from_schema(load_fake_gw_schema())


def test_fingerprint_changes_when_a_field_grison_writes_to_disappears() -> None:
    """The fingerprint is sensitive to the fields grison actually depends on
    (not just any schema churn) — this is what makes the cache trustworthy: a
    schema change that matters always produces a new fingerprint."""
    baseline = fingerprint_from_schema(load_fake_gw_schema())
    sdl = Path("tests/fixtures/gw-schema-7.2.6.graphql").read_text(encoding="utf-8")
    mutated = sdl.replace(
        "type evidence {\n  caption: String!",
        "type evidence {\n  captionRenamed: String!",
    )
    assert mutated != sdl, "fixture no longer contains the expected `evidence.caption` field"
    from graphql import build_schema

    mutated_fp = fingerprint_from_schema(build_schema(mutated))
    assert mutated_fp != baseline


def test_per_sync_cost_with_a_warm_cache_stays_well_under_a_second(
    gw: FakeGhostwriter, gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """Measures the actual warm-path wall time against the fake — the number
    this check exists to fix (originally ~10s/sync from a full introspection +
    validate-every-operation pass executed by the fake over the 1.1MB schema).
    """
    save_cache(tmp_path, fingerprint_from_schema(load_fake_gw_schema()))
    start = time.monotonic()
    check_ghostwriter_compatibility(gw_client, tmp_path)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"warm compat check took {elapsed:.3f}s against the fake"


def test_introspection_denied_is_wrapped_as_a_compat_error(
    gw: FakeGhostwriter, gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """Bug fix: BEFORE the fix, a ``GhostwriterError`` raised by
    ``introspect_schema()`` (a GraphQL authorization error, e.g. a restricted API
    token) escaped ``check_ghostwriter_compatibility`` unwrapped — ``grison.cli``'s
    ``sync`` only catches ``SchemaCompatibilityError`` narrowly (ENGINE.md §10: a
    failed compat check is "could not run", exit 2), so the bare
    ``GhostwriterError`` fell through to the catch-all and exited 1 instead. It is
    now wrapped into ``SchemaCompatibilityError`` here, carrying the cause's own
    message."""
    assert load_cache(tmp_path) is None  # no cache -> always the cold path
    gw.inject_graphql_error("__schema", "not authorized to introspect this schema")
    with pytest.raises(SchemaCompatibilityError, match="not authorized to introspect"):
        check_ghostwriter_compatibility(gw_client, tmp_path)


def test_transport_failure_during_the_probe_is_wrapped_as_a_compat_error(
    gw: FakeGhostwriter, gw_client: GhostwriterClient, tmp_path: Path,
) -> None:
    """Same fix: a transport failure surviving retries (four straight HTTP 503s,
    exhausting the client's default ``max_attempts``) on the cheap
    ``fingerprint_probe()`` request — the very first thing this check does,
    warm-cache or not — is wrapped too."""
    gw.inject_http_status(503, times=4)
    with pytest.raises(SchemaCompatibilityError, match="503"):
        check_ghostwriter_compatibility(gw_client, tmp_path)


def test_fingerprint_of_is_order_independent_of_dict_construction() -> None:
    """Sanity check on the hashing seam: fingerprint_of hashes canonically (used
    by grison.hashing.digest, sort_keys=True), so equal content always yields
    the same fingerprint regardless of key insertion order."""
    a = fingerprint_of({"x": 1, "y": 2})
    b = fingerprint_of({"y": 2, "x": 1})
    assert a == b
