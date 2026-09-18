"""Server compatibility check (BRIEF task F, ENGINE.md "check server
compatibility (schema/query check, versions)") — run once, before the first
fetch of any sync.

Two paths:

* **Warm (the common case).** One cheap request —
  :meth:`grison.remote.ghostwriter.GhostwriterClient.fingerprint_probe`, naming
  every ``query_root``/``mutation_root`` field (with mutation argument names)
  plus the handful of object types grison writes to — hashed and compared
  against ``.grison/state/schema.json``'s cached fingerprint. A match means
  "nothing grison depends on has moved since the last full check", and the
  sync proceeds having made exactly that one request.
* **Cold (fingerprint absent or changed).** The full introspection result is
  fetched, turned into a schema with ``graphql-core``'s ``build_client_schema``,
  and EVERY GraphQL operation :mod:`grison.remote.ghostwriter` sends is
  validated against it — the same "every operation grison sends" extraction
  ``tests/test_gw_schema_conformance.py`` uses, so an incompatible Ghostwriter
  (or a grison regression) is caught with one clear, actionable error naming
  the first offending field/argument, instead of a confusing mid-sync GraphQL
  error on whichever operation happens to run first first (the historical bug
  this check exists to catch: ``fetch_evidence``'s ``findingId`` break, which
  used to surface only when the findings phase got around to evidence).
  Success caches the new fingerprint so the next sync is warm again; failure
  caches nothing, so the next sync re-checks rather than trusting a schema it
  just rejected.

This replaces running the cold path on every single sync (the original
implementation, which cost a full introspection + validate-every-operation
pass over a >1MB schema document on every ``grison sync`` — see
``tests/test_remote_compat.py``'s cost-measuring tests for the before/after
numbers against the fake).

The minimum supported version is expressed as SCHEMA FEATURES, not a version
string grison trusts blindly: ``uploadEvidence`` must accept a ``report``
argument, and ``evidence`` must have no ``findingId`` field (grison's queries
already encode exactly this — see ``grison.remote.ghostwriter``'s
``_UPLOAD_EVIDENCE_MUTATION``/``_EVIDENCE_QUERY``) — "Ghostwriter >= 7.2.0" is
only ever named in the error message, as the human-readable label for what
those features mean, never checked as a version number the server reports
(Ghostwriter's GraphQL API has no version-string field to check anyway).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graphql import GraphQLSchema, build_client_schema, execute_sync, parse, validate

import grison.remote.ghostwriter as gw_module
from grison.errors import GrisonError
from grison.fsio import atomic_write_text, ensure_private_dir
from grison.hashing import digest
from grison.remote.ghostwriter import GhostwriterClient

MIN_GHOSTWRITER_VERSION = "7.2.0"
SCHEMA_CACHE_RELATIVE_PATH = ".grison/state/schema.json"


class SchemaCompatibilityError(GrisonError, RuntimeError):
    """The live Ghostwriter schema doesn't match what grison's GraphQL
    operations assume. ENGINE.md §10: this is a "could not run" failure (exit
    code 2), never a per-record one — the caller must raise/propagate this
    BEFORE the first fetch, not catch it into a per-phase error."""


def _module_level_operations() -> dict[str, str]:
    """Every module-level GraphQL operation string in
    :mod:`grison.remote.ghostwriter` — a private constant (leading ``_``) whose
    value is a query/mutation document. The exact same extraction
    ``tests/test_gw_schema_conformance.py`` uses, so the two can never silently
    drift about what "every operation grison sends" means. The fingerprint
    probe itself is deliberately named to fall outside this scan (see its
    docstring in :mod:`grison.remote.ghostwriter`) — it is a compat-check
    implementation detail, not part of the CRUD surface being conformance
    checked."""
    return {
        name: value
        for name, value in vars(gw_module).items()
        if name.startswith("_")
        and name.endswith(("_QUERY", "_MUTATION"))
        and isinstance(value, str)
    }


def fingerprint_of(probe: dict[str, Any]) -> str:
    """The cache key: a content hash of a fingerprint probe response (live or
    computed offline against an SDL-built schema — see
    :func:`fingerprint_from_schema`)."""
    return digest(probe)


def fingerprint_from_schema(schema: GraphQLSchema) -> str:
    """The same fingerprint, computed with no live server: executes the exact
    probe query against a schema built from SDL text (``graphql-core``
    resolves ``__type``/introspection meta-fields from schema metadata alone —
    no root value or resolver needed). Lets a workspace fixture pre-seed a warm
    cache from ``tests/fixtures/gw-schema-7.2.6.graphql`` so e2e tests stay on
    the warm (one-request) path, while at least two tests still exercise the
    cold path deliberately."""
    result = execute_sync(schema, parse(gw_module._FINGERPRINT_PROBE))
    if result.errors:
        raise SchemaCompatibilityError(str(result.errors[0]))
    return fingerprint_of(dict(result.data or {}))


@dataclass(frozen=True)
class _Cache:
    fingerprint: str
    checked_at: str


def _cache_path(root: Path) -> Path:
    return root / SCHEMA_CACHE_RELATIVE_PATH


def load_cache(root: Path) -> _Cache | None:
    """``None`` on anything short of a well-formed cache (missing file, bad
    JSON, no ``fingerprint`` key) — a corrupt/missing cache is never a crash,
    just a forced cold path, same policy as every other private state file."""
    try:
        raw = json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("fingerprint"), str):
        return None
    return _Cache(fingerprint=raw["fingerprint"], checked_at=str(raw.get("checked_at", "")))


def save_cache(root: Path, fingerprint: str) -> None:
    path = _cache_path(root)
    ensure_private_dir(path.parent)
    atomic_write_text(
        path,
        json.dumps({"fingerprint": fingerprint, "checked_at": datetime.now(UTC).isoformat()}),
        private=True,
    )


def check_ghostwriter_compatibility(client: GhostwriterClient, root: Path) -> None:
    """Warm path: one fingerprint request, compared to ``root``'s cache. Cold
    path (fingerprint absent or changed): full introspection + validate every
    operation, then cache the new fingerprint on success. Raises
    :class:`SchemaCompatibilityError` naming the first offending field/argument
    on any mismatch; returns silently otherwise."""
    fingerprint = fingerprint_of(client.fingerprint_probe())
    cached = load_cache(root)
    if cached is not None and cached.fingerprint == fingerprint:
        return
    _validate_every_operation(client)
    save_cache(root, fingerprint)


def _validate_every_operation(client: GhostwriterClient) -> None:
    raw = client.introspect_schema()
    schema = build_client_schema(raw)
    operations = _module_level_operations()
    for name, source in sorted(operations.items()):
        errors = validate(schema, parse(source))
        if errors:
            first = errors[0]
            raise SchemaCompatibilityError(
                f"Ghostwriter schema mismatch: {first.message} (operation {name}) — "
                f"grison {_grison_version()} needs Ghostwriter >= {MIN_GHOSTWRITER_VERSION}"
            )


def _grison_version() -> str:
    from grison import __version__

    return __version__
