"""An in-memory fake Ghostwriter GraphQL server, typed by the real 7.2.6 schema.

Every request is parsed and validated against ``tests/fixtures/gw-schema-7.2.6.graphql``
(the introspected SDL of a real Ghostwriter 7.2.6, see ``lab/LAB.md`` in the rework repo)
with ``graphql-core`` before it ever touches a resolver — a query or mutation that would
be rejected by the real server (wrong field, wrong argument, missing required argument)
is rejected here too, with the same error shape Hasura returns: HTTP 200 with
``{"errors": [{"message": ...}]}``. Two grison operations are known to fail this way
today (``GhostwriterClient.fetch_evidence`` selects a ``findingId`` field that does not
exist on ``evidence``, and ``upload_evidence`` sends a ``finding`` argument
``uploadEvidence`` does not accept) — that is a real production break, not a fake-fidelity
gap; see ``tests/test_gw_schema_conformance.py``.

Usage::

    gw = FakeGhostwriter()
    gw.seed_finding(id=1, title="SQLi", severity_id=5, finding_type_id=4)
    client = GhostwriterClient(creds, transport=gw.transport)
    ...
    assert gw.operation_log[-1].name == "update_finding_by_pk"
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from graphql import (
    GraphQLError,
    GraphQLSchema,
    OperationType,
    build_schema,
    execute_sync,
)
from graphql import (
    parse as gql_parse,
)
from graphql import (
    validate as gql_validate,
)
from graphql.error import GraphQLSyntaxError
from graphql.language import OperationDefinitionNode

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "gw-schema-7.2.6.graphql"


@lru_cache(maxsize=1)
def load_schema() -> GraphQLSchema:
    """Build (once, cached) the real 7.2.6 schema. Immutable and resolver-free — every
    :class:`FakeGhostwriter` instance supplies its own store via a per-instance
    ``field_resolver`` rather than mutating the shared schema object, so fakes used
    concurrently in different tests never interfere with each other."""
    return build_schema(SCHEMA_PATH.read_text(encoding="utf-8"))


# --- Hasura-style error translation -----------------------------------------------
#
# graphql-core's own validation messages are close to Hasura's but not identical.
# Confirmed against the real lab instance (SSL_CERT_FILE=lab/lab-ca.pem, GW 7.2.6):
#
#   query { evidence { findingId } }
#     -> {"errors":[{"message":
#          "field 'findingId' not found in type: 'evidence'", ...}]}
#   mutation { uploadEvidence(finding: 1, ...) { id } }
#     -> {"errors":[{"message":
#          "'uploadEvidence' has no argument named 'finding'", ...}]}
#   query { nonexistentField { id } }
#     -> {"errors":[{"message":
#          "field 'nonexistentField' not found in type: 'query_root'", ...}]}
#   (bad bearer token)
#     -> {"errors":[{"message":
#          "Authentication hook unauthorized this request", ...}]}
#
# graphql-core 3.2's own message for the first two (raw, single-quoted already):
#   "Cannot query field 'findingId' on type 'evidence'."
#   "Unknown argument 'finding' on field 'mutation_root.uploadEvidence'."

_UNKNOWN_FIELD_RE = re.compile(r"Cannot query field '(?P<field>[^']+)' on type '(?P<type>[^']+)'")
_UNKNOWN_ARG_RE = re.compile(
    r"Unknown argument '(?P<arg>[^']+)' on field '(?:[\w]+\.)?(?P<field>[^']+)'"
)


def _hasura_message(message: str) -> str:
    """Translate a graphql-core validation message into Hasura's wording, where a real
    shape is known (see the module docstring above); anything else passes through
    verbatim (still a real GraphQL validation failure, just not one whose exact
    Hasura wording has been captured from the lab)."""
    m = _UNKNOWN_FIELD_RE.search(message)
    if m:
        return f"field '{m.group('field')}' not found in type: '{m.group('type')}'"
    m = _UNKNOWN_ARG_RE.search(message)
    if m:
        return f"'{m.group('field')}' has no argument named '{m.group('arg')}'"
    return message


_AUTH_DENIED = {
    "errors": [
        {
            "message": "Authentication hook unauthorized this request",
            "extensions": {"path": "$", "code": "access-denied"},
        }
    ]
}


@dataclass(frozen=True)
class LoggedOperation:
    """One mutation the fake executed, in call order."""

    name: str
    variables: dict[str, Any]


@dataclass
class _Injected:
    kind: str  # "http500" | "timeout" | "graphql_error" | "http_status"
    op: str | None = None  # only for graphql_error: the root field name to hit
    message: str = ""
    status: int = 500  # only for http_status: the response code to return (e.g. 429/503)
    retry_after: str | None = None  # only for http_status: sent as the Retry-After header


def _matches_where(row: dict, where: dict | None) -> bool:
    """The Hasura ``where`` argument subset grison uses or will plausibly use:
    ``_and``/``_or``/``_not`` combinators and ``_eq``/``_in``/``_neq``/``_is_null``
    per-column operators."""
    if not where:
        return True
    for key, cond in where.items():
        if key == "_and":
            if not all(_matches_where(row, c) for c in cond):
                return False
        elif key == "_or":
            if not any(_matches_where(row, c) for c in cond):
                return False
        elif key == "_not":
            if _matches_where(row, cond):
                return False
        elif isinstance(cond, dict):
            val = row.get(key)
            for op, expected in cond.items():
                if op == "_eq" and val != expected:
                    return False
                elif op == "_neq" and val == expected:
                    return False
                elif op == "_in" and val not in (expected or []):
                    return False
                elif op == "_nin" and val in (expected or []):
                    return False
                elif op == "_is_null" and (val is None) != bool(expected):
                    return False
                elif op not in ("_eq", "_neq", "_in", "_nin", "_is_null"):
                    raise NotImplementedError(f"fake_gw: unsupported where operator {op!r}")
        else:
            if row.get(key) != cond:
                return False
    return True


def _apply_order_limit_offset(
    rows: list[dict], *, order_by: list[dict] | None, limit: int | None, offset: int | None
) -> list[dict]:
    if order_by:
        for clause in reversed(order_by):
            key, direction = next(iter(clause.items()))
            rows = sorted(rows, key=lambda r, k=key: (r.get(k) is None, r.get(k)),
                           reverse=(direction == "desc"))
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return rows


class GhostwriterFakeError(RuntimeError):
    """Raised by a resolver for a constraint the fake enforces (unique friendlyName,
    missing report, unknown id, …) — surfaced to the client as an ordinary GraphQL
    execution error, same as a real Hasura/Postgres constraint violation would be."""


class GWStore:
    """The in-memory Ghostwriter database backing :class:`FakeGhostwriter`.

    Every row is a plain ``dict`` keyed by the exact GraphQL field names the schema
    defines (camelCase, e.g. ``severityId``) — graphql-core's default field resolver
    reads dict keys directly, so nested object/relationship fields (``report.project``,
    ``project.client``, …) resolve with no custom code at all as long as the seeded
    dict shape matches the schema's field names.
    """

    def __init__(self) -> None:
        self.findings: list[dict] = []
        self.reported_findings: list[dict] = []
        self.evidence: list[dict] = []
        self.evidence_bytes: dict[int, bytes] = {}
        self.reports: list[dict] = []
        self.finding_severities: list[dict] = []
        self.finding_types: list[dict] = []
        self.django_content_types: list[dict] = []
        self.tagged_items: list[dict] = []  # {content_type_id, object_id, tag: {name}}
        self.users: list[dict] = []
        self.project_notes: list[dict] = []
        self.extra_field_specs: list[dict] = []
        self.whoami_username = "grison-lab"
        self._next_ids: dict[str, int] = {}
        self.operation_log: list[LoggedOperation] = []

    # --- id / seeding helpers ----------------------------------------------------

    def _next_id(self, kind: str, *, start: int = 1000) -> int:
        current = self._next_ids.get(kind, start)
        self._next_ids[kind] = current + 1
        return current

    def seed_defaults(self) -> None:
        """The lookup tables + content types every sync needs: the 5 severities / 7
        finding types matching grison's own hardcoded ``gw_id`` maps
        (``grison.model.enums``) — without these, ``check_severity_drift`` /
        ``check_finding_type_drift`` abort every sync before touching a record — plus
        the ``djangoContentType`` rows the tag mechanism resolves against, and one
        admin user for ``whoami``."""
        self.finding_severities = [
            {"id": 1, "severity": "Informational", "weight": 1},
            {"id": 2, "severity": "Low", "weight": 2},
            {"id": 3, "severity": "Medium", "weight": 3},
            {"id": 4, "severity": "High", "weight": 4},
            {"id": 5, "severity": "Critical", "weight": 5},
        ]
        self.finding_types = [
            {"id": 1, "findingType": "Network"},
            {"id": 2, "findingType": "Physical"},
            {"id": 3, "findingType": "Wireless"},
            {"id": 4, "findingType": "Web"},
            {"id": 5, "findingType": "Mobile"},
            {"id": 6, "findingType": "Cloud"},
            {"id": 7, "findingType": "Host"},
        ]
        self.django_content_types = [
            {"id": 40, "appLabel": "reporting", "model": "finding"},
            {"id": 41, "appLabel": "reporting", "model": "reportfindinglink"},
        ]
        self.users = [{"id": 1, "username": self.whoami_username, "name": ""}]
        self.seed_report_extra_field_specs()

    def seed_finding(self, **fields: Any) -> dict:
        row = {
            "id": self._next_id("finding"),
            "title": "Untitled finding",
            "severityId": 3,
            "findingTypeId": 4,
            "cvssScore": None,
            "cvssVector": "",
            "description": "",
            "impact": "",
            "mitigation": "",
            "references": "",
            "replication_steps": "",
        }
        row.update(fields)
        self.findings.append(row)
        return row

    def seed_reported_finding(self, **fields: Any) -> dict:
        row = {
            "id": self._next_id("reportedFinding"),
            "reportId": None,
            "title": "Untitled finding",
            "severityId": 3,
            "findingTypeId": 4,
            "cvssScore": None,
            "cvssVector": "",
            "description": "",
            "impact": "",
            "mitigation": "",
            "references": "",
            "replication_steps": "",
            "affectedEntities": "",
        }
        row.update(fields)
        self.reported_findings.append(row)
        return row

    def seed_evidence(self, **fields: Any) -> dict:
        """``reportId`` is required (D1: evidence belongs to a report, not a finding —
        the real schema has no ``finding``/``findingId`` column on ``evidence`` at all)."""
        if fields.get("reportId") is None:
            raise ValueError("seed_evidence requires reportId (evidence has no finding link)")
        content = fields.pop("content", b"fake-image-bytes")
        row = {
            "id": self._next_id("evidence"),
            "reportId": fields["reportId"],
            "document": f"evidence/{fields['reportId']}/file.png",
            "caption": "",
            "friendlyName": "",
            "description": "",
            "uploadDate": date.today().isoformat(),
        }
        row.update(fields)
        self.evidence.append(row)
        self.evidence_bytes[row["id"]] = content
        return row

    def seed_report(self, *, project: dict | None = None, **fields: Any) -> dict:
        """``project`` (and its nested ``client``) merge over sensible non-null
        defaults rather than replacing the whole sub-object — the schema requires
        ``project``/``project.client`` and most of their scalar columns to be
        non-null, so a caller overriding just ``project={"codename": "OP-X"}`` must
        not accidentally null out ``project.id``/``client.id``/etc."""
        pid = self._next_id("project")
        client = {"id": self._next_id("client"), "name": "Acme Test Corp", "shortName": "ACME"}
        client.update((project or {}).get("client") or {})
        proj = {
            "id": pid,
            "startDate": "2026-01-01",
            "endDate": "2026-01-14",
            "codename": "OP-TEST",
            "collab_note": "",
            "scopes": [],
            "objectives": [],
            "targets": [],
            "whitecards": [],
            "comments": [],
        }
        proj.update({k: v for k, v in (project or {}).items() if k != "client"})
        proj["client"] = client
        row = {
            "id": self._next_id("report"),
            "title": "Untitled report",
            "complete": False,
            "archived": False,
            "delivered": False,
            "creation": date.today().isoformat(),
            "last_update": date.today().isoformat(),
            "extraFields": {},
            "project": proj,
        }
        row.update(fields)
        self.reports.append(row)
        return row

    _DEFAULT_REPORT_FIELDS = (
        "about_us", "executive_summary", "attack_chain", "methodology", "disclaimer",
        "scope_text", "appendix",
    )

    def seed_report_extra_field_specs(self, fields: tuple[str, ...] | None = None) -> list[dict]:
        """The Report model's instance-defined rich-text ``extraFieldSpec`` rows —
        production-shaped by default (the real lab's own 7 fields, in the same
        order: ``about_us, executive_summary, attack_chain, methodology,
        disclaimer, scope_text, appendix`` — see ``lab/LAB.md`` and
        ``lab/seed/seed_gw.py`` in the rework repo). Called once by
        :meth:`seed_defaults`; a test that needs a different field set calls this
        again after clearing ``self.extra_field_specs`` first."""
        rows = [
            {
                "id": self._next_id("extraFieldSpec", start=3), "internalName": name,
                "displayName": name.replace("_", " ").title(), "position": i + 1,
                "targetModel": "reporting.Report",
            }
            for i, name in enumerate(fields or self._DEFAULT_REPORT_FIELDS)
        ]
        self.extra_field_specs.extend(rows)
        return rows

    def seed_project_note(self, *, project_id: int, **fields: Any) -> dict:
        """A ``projectNote`` row, kept in sync with every seeded report's
        denormalized ``project.comments`` list (this fake has no live relational
        join — see the module docstring's note on dict-shaped rows)."""
        user = fields.pop("user", {"name": "", "username": self.whoami_username})
        row = {
            "id": self._next_id("projectNote"), "projectId": project_id,
            "note": "", "operatorId": 1, "timestamp": date.today().isoformat(), "user": user,
        }
        row.update(fields)
        self.project_notes.append(row)
        for rec in self.reports:
            if (rec.get("project") or {}).get("id") == project_id:
                rec["project"].setdefault("comments", []).append(row)
        return row

    def seed_tag(self, table: str, object_id: int, name: str) -> None:
        """``table`` is grison's own vocabulary (``"finding"``/``"reportedFinding"``) —
        mapped to a content-type id the same way :meth:`GhostwriterClient
        .fetch_tag_map` expects (``reportedFinding`` -> content type ``reportfindinglink``)."""
        model = "finding" if table == "finding" else "reportfindinglink"
        ct = next(r for r in self.django_content_types if r["model"] == model)
        self.tagged_items.append(
            {"content_type_id": ct["id"], "object_id": object_id, "tag": {"name": name}}
        )

    # --- lookups -------------------------------------------------------------

    def _by_id(self, rows: list[dict], ident: int) -> dict | None:
        return next((r for r in rows if r["id"] == ident), None)

    def report_exists(self, report_id: int) -> bool:
        return self._by_id(self.reports, report_id) is not None

    def _log(self, name: str, variables: dict) -> None:
        self.operation_log.append(LoggedOperation(name, dict(variables)))


class FakeGhostwriter:
    """An in-memory Ghostwriter GraphQL server exposed as an ``httpx.MockTransport``
    handler — schema-typed against the real 7.2.6 SDL (see module docstring)."""

    def __init__(self, *, token: str = "test-gw-token", seed_defaults: bool = True) -> None:
        self.store = GWStore()
        if seed_defaults:
            self.store.seed_defaults()
        self.token = token
        self._schema = load_schema()
        self._pending: list[_Injected] = []
        self.request_log: list[LoggedOperation] = []  # every operation, query or mutation
        self._call_counts: dict[str, int] = {}
        self._hooks: dict[str, list[tuple[int, Any]]] = {}

    def on_request(self, operation: str, callback: Any, *, call_number: int = 1) -> None:
        """Run ``callback()`` immediately before ``operation``'s ``call_number``-th
        invocation this fake's lifetime is resolved — for simulating a concurrent
        remote edit landing between two calls within the same sync run (e.g. between
        the top-of-run ``fetch_reports()`` snapshot and a report's own pre-push
        refetch). Mirrors the project's own ``FakeGW.on_fetch`` pattern used in
        ``tests/test_reports_guards.py``, generalized to any operation."""
        self._hooks.setdefault(operation, []).append((call_number, callback))

    def call_count(self, operation: str) -> int:
        """How many times ``operation`` (a root field name, e.g. ``"report"``) has
        been resolved so far — the baseline :meth:`on_request` callers use to target
        a call relative to "now" instead of hardcoding an absolute lifetime count."""
        return self._call_counts.get(operation, 0)

    # --- transport -------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        injected = (
            self._pop_injection(kind="http500")
            or self._pop_injection(kind="timeout")
            or self._pop_injection(kind="http_status")
        )
        if injected is not None and injected.kind == "timeout":
            raise httpx.TimeoutException("fake_gw: injected timeout", request=request)
        if injected is not None and injected.kind == "http500":
            return httpx.Response(500, text="fake_gw: injected internal server error")
        if injected is not None and injected.kind == "http_status":
            headers = {"Retry-After": injected.retry_after} if injected.retry_after else {}
            return httpx.Response(
                injected.status, text=f"fake_gw: injected {injected.status}", headers=headers
            )

        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            return httpx.Response(200, json=_AUTH_DENIED)

        body = json.loads(request.content.decode("utf-8"))
        query: str = body.get("query", "")
        variables: dict = body.get("variables") or {}

        try:
            document = gql_parse(query)
        except GraphQLSyntaxError as e:
            return httpx.Response(200, json={"errors": [{"message": str(e)}]})

        errors = gql_validate(self._schema, document)
        if errors:
            payload = {"errors": [self._format_validation_error(e) for e in errors]}
            return httpx.Response(200, json=payload)

        op_def = document.definitions[0]
        root_field = _first_root_field(op_def)
        is_mutation = (
            isinstance(op_def, OperationDefinitionNode)
            and op_def.operation == OperationType.MUTATION
        )

        graphql_error_injection = self._pop_injection(kind="graphql_error", op=root_field)
        if graphql_error_injection is not None:
            return httpx.Response(
                200, json={"errors": [{"message": graphql_error_injection.message}]}
            )

        if root_field is not None:
            count = self._call_counts[root_field] = self._call_counts.get(root_field, 0) + 1
            for call_number, callback in self._hooks.get(root_field, []):
                if call_number == count:
                    callback()
            self.request_log.append(LoggedOperation(root_field, dict(variables)))
            if is_mutation:
                self.store._log(root_field, variables)

        result = execute_sync(
            self._schema,
            document,
            root_value=self.store,
            variable_values=variables,
            field_resolver=self._resolve_field,
        )
        payload = {}
        if result.errors:
            payload["errors"] = [self._format_exec_error(e) for e in result.errors]
        if result.data is not None:
            payload["data"] = result.data
        return httpx.Response(200, json=payload)

    def _format_validation_error(self, err: GraphQLError) -> dict:
        return {
            "message": _hasura_message(err.message),
            "extensions": {"code": "validation-failed"},
        }

    def _format_exec_error(self, err: GraphQLError) -> dict:
        original = err.original_error
        message = str(original) if original is not None else err.message
        return {"message": message, "extensions": {"code": "unexpected"}}

    # --- resolution --------------------------------------------------------

    def _resolve_field(self, source: Any, info: Any, **args: Any) -> Any:
        parent = info.parent_type.name
        name = info.field_name
        if parent == "query_root":
            return self._resolve_query(name, args)
        if parent == "mutation_root":
            return self._resolve_mutation(name, args)
        if isinstance(source, dict):
            return source.get(name)
        return getattr(source, name, None)

    def _resolve_query(self, name: str, args: dict) -> Any:  # noqa: PLR0911
        store = self.store
        if name == "finding":
            rows = [r for r in store.findings if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=args.get("limit"),
                offset=args.get("offset"),
            )
        if name == "finding_by_pk":
            return store._by_id(store.findings, args["id"])
        if name == "reportedFinding":
            rows = [r for r in store.reported_findings if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=args.get("limit"),
                offset=args.get("offset"),
            )
        if name == "reportedFinding_by_pk":
            return store._by_id(store.reported_findings, args["id"])
        if name == "evidence":
            rows = [r for r in store.evidence if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=args.get("limit"),
                offset=args.get("offset"),
            )
        if name == "report":
            rows = [r for r in store.reports if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=args.get("limit"),
                offset=args.get("offset"),
            )
        if name == "report_by_pk":
            return store._by_id(store.reports, args["id"])
        if name == "extraFieldSpec":
            rows = [r for r in store.extra_field_specs if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=args.get("limit"),
                offset=args.get("offset"),
            )
        if name == "projectNote_by_pk":
            return store._by_id(store.project_notes, args["id"])
        if name == "findingSeverity":
            return store.finding_severities
        if name == "findingType":
            return store.finding_types
        if name == "djangoContentType":
            return [r for r in store.django_content_types if _matches_where(r, args.get("where"))]
        if name == "taggedItem":
            rows = [r for r in store.tagged_items if _matches_where(r, args.get("where"))]
            return _apply_order_limit_offset(
                rows, order_by=args.get("order_by"), limit=None, offset=None
            )
        if name == "user":
            return [r for r in store.users if _matches_where(r, args.get("where"))]
        if name == "whoami":
            return {"username": store.whoami_username, "role": "admin", "expires": None}
        if name == "downloadEvidence":
            ev = store._by_id(store.evidence, args["evidenceId"])
            if ev is None:
                raise GhostwriterFakeError(f"evidence id {args['evidenceId']} does not exist")
            data = store.evidence_bytes.get(ev["id"], b"")
            return {
                "fileBase64": base64.b64encode(data).decode(),
                "filename": Path(ev["document"]).name,
                "friendlyName": ev["friendlyName"],
                "evidenceId": ev["id"],
                "downloadUrl": f"/media/{ev['document']}",
            }
        raise NotImplementedError(f"fake_gw: no query resolver for {name!r} — add one")

    def _resolve_mutation(self, name: str, args: dict) -> Any:  # noqa: PLR0911
        store = self.store
        if name == "insert_finding_one":
            return store.seed_finding(**args["object"])
        if name == "update_finding_by_pk":
            row = store._by_id(store.findings, args["pk_columns"]["id"])
            if row is None:
                return None
            row.update(args["_set"])
            return row
        if name == "delete_finding_by_pk":
            row = store._by_id(store.findings, args["id"])
            if row is not None:
                store.findings.remove(row)
            return row
        if name == "insert_reportedFinding_one":
            return store.seed_reported_finding(**args["object"])
        if name == "update_reportedFinding_by_pk":
            row = store._by_id(store.reported_findings, args["pk_columns"]["id"])
            if row is None:
                return None
            row.update(args["_set"])
            return row
        if name == "delete_reportedFinding_by_pk":
            row = store._by_id(store.reported_findings, args["id"])
            if row is not None:
                # deleting a report's finding does NOT delete its evidence (D1: evidence
                # belongs to the report, not the finding — no cascade exists to break).
                store.reported_findings.remove(row)
            return row
        if name == "uploadEvidence":
            return self._upload_evidence(**args)
        if name == "update_evidence_by_pk":
            row = store._by_id(store.evidence, args["pk_columns"]["id"])
            if row is None:
                return None
            row.update(args["_set"])
            return row
        if name == "delete_evidence_by_pk":
            row = store._by_id(store.evidence, args["id"])
            if row is not None:
                store.evidence.remove(row)
                store.evidence_bytes.pop(row["id"], None)
            return row
        if name == "setTags":
            model = args["model"]  # "finding" | "report_finding_link"
            ct_model = "finding" if model == "finding" else "reportfindinglink"
            ct = next((r for r in store.django_content_types if r["model"] == ct_model), None)
            if ct is not None:
                store.tagged_items = [
                    t for t in store.tagged_items
                    if not (t["content_type_id"] == ct["id"] and t["object_id"] == args["id"])
                ]
                for tag in args["tags"]:
                    store.tagged_items.append(
                        {"content_type_id": ct["id"], "object_id": args["id"], "tag": {"name": tag}}
                    )
            return {"tags": args["tags"]}
        if name == "update_report_by_pk":
            row = store._by_id(store.reports, args["pk_columns"]["id"])
            if row is None:
                return None
            row.update(args["_set"])
            return row
        if name == "insert_projectNote_one":
            obj = args["object"]
            username = next(
                (u["username"] for u in store.users if u["id"] == obj.get("operatorId")), None
            )
            row = {
                "id": store._next_id("projectNote"),
                "projectId": obj.get("projectId"),
                "note": obj.get("note"),
                "operatorId": obj.get("operatorId"),
                "timestamp": obj.get("timestamp"),
                "user": {"name": "", "username": username or ""},
            }
            store.project_notes.append(row)
            for rec in store.reports:
                if (rec.get("project") or {}).get("id") == row["projectId"]:
                    rec["project"].setdefault("comments", []).append(row)
            return {"id": row["id"]}
        if name == "delete_projectNote_by_pk":
            row = store._by_id(store.project_notes, args["id"])
            if row is not None:
                store.project_notes.remove(row)
                for rec in store.reports:
                    comments = (rec.get("project") or {}).get("comments")
                    if comments is not None:
                        rec["project"]["comments"] = [c for c in comments if c["id"] != row["id"]]
            return row
        if name == "generateReport":
            report = store._by_id(store.reports, args["id"])
            if report is None:
                raise GhostwriterFakeError(f"report id {args['id']} does not exist")
            return {"id": report["id"]}
        raise NotImplementedError(f"fake_gw: no mutation resolver for {name!r} — add one")

    def _upload_evidence(
        self, *, report: int, filename: str, caption: str, friendly_name: str,
        file_base64: str, description: str | None = None, tags: str | None = None,
    ) -> dict:
        store = self.store
        if not store.report_exists(report):
            raise GhostwriterFakeError(
                f'insert or update on table "evidence" violates foreign key constraint '
                f'"evidence_report_id_fkey" — report {report} does not exist'
            )
        if any(
            e["reportId"] == report and e["friendlyName"] == friendly_name
            for e in store.evidence
        ):
            raise GhostwriterFakeError(
                f"Uniqueness violation. duplicate key value violates unique constraint "
                f"— friendlyName {friendly_name!r} already used in report {report}"
            )
        ev_id = store._next_id("evidence")
        row = {
            "id": ev_id,
            "reportId": report,
            "document": f"evidence/{report}/{filename}",
            "caption": caption or "",
            "friendlyName": friendly_name,
            "description": description or "",
            "uploadDate": date.today().isoformat(),
        }
        store.evidence.append(row)
        store.evidence_bytes[ev_id] = base64.b64decode(file_base64)
        return {"id": ev_id}

    # --- failure injection ---------------------------------------------------

    def inject_http_500(self, times: int = 1) -> None:
        """The next ``times`` requests get HTTP 500 instead of being processed."""
        for _ in range(times):
            self._pending.append(_Injected("http500"))

    def inject_timeout(self, times: int = 1) -> None:
        """The next ``times`` requests raise ``httpx.TimeoutException`` instead of
        being processed."""
        for _ in range(times):
            self._pending.append(_Injected("timeout"))

    def inject_http_status(
        self, status: int, *, times: int = 1, retry_after: str | None = None
    ) -> None:
        """The next ``times`` requests get HTTP ``status`` (e.g. 429 or 503) instead
        of being processed; ``retry_after``, if given, is sent as the response's
        ``Retry-After`` header."""
        for _ in range(times):
            self._pending.append(_Injected("http_status", status=status, retry_after=retry_after))

    def inject_graphql_error(self, operation: str, message: str, *, times: int = 1) -> None:
        """The next ``times`` requests whose root selection is ``operation`` (e.g.
        ``"update_finding_by_pk"``) get a GraphQL ``errors`` response with ``message``
        instead of being executed. Queued independently per operation name."""
        for _ in range(times):
            self._pending.append(_Injected("graphql_error", op=operation, message=message))

    def _pop_injection(self, *, kind: str, op: str | None = None) -> _Injected | None:
        for i, item in enumerate(self._pending):
            if item.kind != kind:
                continue
            if kind == "graphql_error" and item.op != op:
                continue
            return self._pending.pop(i)
        return None

    # --- inspection ------------------------------------------------------------

    @property
    def operation_log(self) -> list[LoggedOperation]:
        """Every mutation executed, in call order, with its variables."""
        return self.store.operation_log


def _first_root_field(op_def: Any) -> str | None:
    try:
        selection = op_def.selection_set.selections[0]
        return selection.name.value
    except (AttributeError, IndexError):
        return None
