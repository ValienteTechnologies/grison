"""Read-only Ghostwriter GraphQL client.

Ghostwriter exposes a Hasura endpoint at ``{gw_url}/v1/graphql``. GW (and
BookStack) sit behind Cloudflare Access, so every request carries the bearer
token plus the CF service-token header pair (see :mod:`grison.remote.creds`).
"""

from __future__ import annotations

import base64

import httpx

from grison.remote.creds import Creds

_FINDING_QUERY = """
query {
  finding {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_REPORTED_FINDING_QUERY = """
query {
  reportedFinding {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
  }
}
"""

_EVIDENCE_QUERY = """
query {
  evidence {
    id
    findingId
    reportId
    document
    caption
    friendlyName
    description
  }
}
"""

_FINDING_SEVERITY_QUERY = """
query {
  findingSeverity {
    id
    severity
    weight
  }
}
"""

_FINDING_TYPE_LOOKUP_QUERY = """
query {
  findingType {
    id
    findingType
  }
}
"""

_CONTENT_TYPES_QUERY = """
query {
  djangoContentType(
    where: {appLabel: {_eq: "reporting"}, model: {_in: ["finding", "reportfindinglink"]}}
  ) {
    id
    model
  }
}
"""

_TAGGED_ITEM_QUERY = """
query($content_type_ids: [Int!]) {
  taggedItem(where: {content_type_id: {_in: $content_type_ids}}, order_by: {id: asc}) {
    content_type_id
    object_id
    tag {
      name
    }
  }
}
"""

_TAGGED_ITEM_FOR_QUERY = """
query($content_type_id: Int!, $object_id: Int!) {
  taggedItem(
    where: {content_type_id: {_eq: $content_type_id}, object_id: {_eq: $object_id}}
    order_by: {id: asc}
  ) {
    tag {
      name
    }
  }
}
"""

_SET_TAGS_MUTATION = """
mutation($id: bigint!, $model: String!, $tags: [String!]!) {
  setTags(id: $id, model: $model, tags: $tags) {
    tags
  }
}
"""

_REPORT_QUERY = """
query {
  report {
    id
    title
    complete
    archived
    delivered
    creation
    last_update
    extraFields
    project {
      id
      startDate
      endDate
      client {
        id
        name
        shortName
      }
    }
  }
}
"""

_UPDATE_REPORT_MUTATION = """
mutation($id: bigint!, $set: report_set_input!) {
  update_report_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
  }
}
"""

_DOWNLOAD_EVIDENCE_QUERY = """
query($id: Int!) {
  downloadEvidence(evidenceId: $id) {
    fileBase64
    filename
  }
}
"""

# The insert/update mutations below return the exact field set _FINDING_QUERY /
# _REPORTED_FINDING_QUERY select (the sync engine rebuilds the canonical local finding
# from this ``returning`` row instead of the pre-push local object — the "returning" set
# governed by the manager role's SELECT permission, verified to cover every field here).
_INSERT_FINDING_MUTATION = """
mutation($obj: finding_insert_input!) {
  insert_finding_one(object: $obj) {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_UPDATE_FINDING_MUTATION = """
mutation($id: bigint!, $set: finding_set_input!) {
  update_finding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_INSERT_REPORTED_FINDING_MUTATION = """
mutation($obj: reportedFinding_insert_input!) {
  insert_reportedFinding_one(object: $obj) {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
  }
}
"""

_UPDATE_REPORTED_FINDING_MUTATION = """
mutation($id: bigint!, $set: reportedFinding_set_input!) {
  update_reportedFinding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
  }
}
"""

_UPLOAD_EVIDENCE_MUTATION = """
mutation(
  $finding: Int!
  $file_base64: String!
  $filename: String!
  $caption: String!
  $friendly_name: String!
  $description: String
) {
  uploadEvidence(
    finding: $finding
    file_base64: $file_base64
    filename: $filename
    caption: $caption
    friendly_name: $friendly_name
    description: $description
  ) {
    id
  }
}
"""

_UPDATE_EVIDENCE_MUTATION = """
mutation($id: bigint!, $set: evidence_set_input!) {
  update_evidence_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    caption
    friendlyName
    description
  }
}
"""

_EVIDENCE_BY_IDS_QUERY = """
query($ids: [bigint!]) {
  evidence(where: {id: {_in: $ids}}) {
    id
    document
    caption
    friendlyName
    description
  }
}
"""

_DELETE_EVIDENCE_MUTATION = """
mutation($id: bigint!) {
  delete_evidence_by_pk(id: $id) {
    id
  }
}
"""

_DELETE_FINDING_MUTATION = """
mutation($id: bigint!) {
  delete_finding_by_pk(id: $id) {
    id
  }
}
"""

_DELETE_REPORTED_FINDING_MUTATION = """
mutation($id: bigint!) {
  delete_reportedFinding_by_pk(id: $id) {
    id
  }
}
"""


class GhostwriterError(RuntimeError):
    """Raised on a non-2xx HTTP response or a GraphQL ``errors`` payload."""


class GhostwriterClient:
    """Thin read-only wrapper over Ghostwriter's Hasura GraphQL endpoint."""

    def __init__(self, creds: Creds, *, timeout: float = 30.0, transport=None) -> None:
        headers = {
            "Authorization": f"Bearer {creds.gw_token}",
            "Content-Type": "application/json",
            **creds.cf_headers(),
        }
        self._client = httpx.Client(
            base_url=creds.gw_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._content_type_ids: dict[str, int] | None = None  # {table: content_type_id}, cached

    def _post(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        resp = self._client.post("/v1/graphql", json=payload)
        if not resp.is_success:
            raise GhostwriterError(
                f"Ghostwriter request failed: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        errors = body.get("errors")
        if errors:
            raise GhostwriterError(f"Ghostwriter GraphQL error: {errors[0].get('message')}")
        return body["data"]

    def fetch_findings(self) -> list[dict]:
        return self._post(_FINDING_QUERY)["finding"]

    def fetch_reported_findings(self) -> list[dict]:
        return self._post(_REPORTED_FINDING_QUERY)["reportedFinding"]

    def fetch_evidence(self) -> list[dict]:
        return self._post(_EVIDENCE_QUERY)["evidence"]

    def fetch_reports(self) -> list[dict]:
        return self._post(_REPORT_QUERY)["report"]

    def fetch_finding_severities(self) -> list[dict]:
        """``{id, severity, weight}`` rows — the live source of truth checked at sync
        start against grison's hardcoded severity gw_id map (severity/finding-type
        drift tripwire)."""
        return self._post(_FINDING_SEVERITY_QUERY)["findingSeverity"]

    def fetch_finding_types(self) -> list[dict]:
        """``{id, findingType}`` rows — the lookup table's name column is camelCase
        ``findingType`` on the live schema (verified by introspection 2026-07-15)."""
        return self._post(_FINDING_TYPE_LOOKUP_QUERY)["findingType"]

    def _resolve_content_types(self) -> dict[str, int]:
        """``{"finding": id, "reportedFinding": id}`` — content-type ids are per-install,
        so they're resolved from ``djangoContentType`` at runtime and cached rather than
        hardcoded. GW's Django model name for a ``reportedFinding`` row is
        ``reportfindinglink`` (no underscore — Django's own ``ContentType.model``
        convention), distinct from the ``report_finding_link`` string ``setTags`` wants."""
        if self._content_type_ids is None:
            rows = self._post(_CONTENT_TYPES_QUERY)["djangoContentType"]
            by_model = {row["model"]: row["id"] for row in rows}
            mapping: dict[str, int] = {}
            if "finding" in by_model:
                mapping["finding"] = by_model["finding"]
            if "reportfindinglink" in by_model:
                mapping["reportedFinding"] = by_model["reportfindinglink"]
            self._content_type_ids = mapping
        return self._content_type_ids

    def fetch_tag_map(self) -> dict[tuple[str, int], list[str]]:
        """``{(table, object_id): [tag names]}`` over both taggable finding tables, in
        the order Ghostwriter returns them (``taggedItem`` ordered by id)."""
        content_types = self._resolve_content_types()
        table_by_ct_id = {ct_id: table for table, ct_id in content_types.items()}
        rows = self._post(
            _TAGGED_ITEM_QUERY, {"content_type_ids": list(content_types.values())}
        )["taggedItem"]
        tag_map: dict[tuple[str, int], list[str]] = {}
        for row in rows:
            table = table_by_ct_id.get(row["content_type_id"])
            if table is None:
                continue
            tag_map.setdefault((table, row["object_id"]), []).append(row["tag"]["name"])
        return tag_map

    def set_tags(self, record_id: int, table: str, tags: list[str]) -> None:
        """Replace-all the tag set on a ``finding``/``reportedFinding`` row — ``setTags``
        is REPLACE-ALL semantics (upstream: ``obj.tags.set(input)``), not additive."""
        model = "finding" if table == "finding" else "report_finding_link"
        self._post(_SET_TAGS_MUTATION, {"id": record_id, "model": model, "tags": tags})

    def fetch_tags_for(self, table: str, object_id: int) -> list[str]:
        """Tag names on exactly one ``finding``/``reportedFinding`` row — used to
        re-derive canonical tag state right after a push. django-taggit is
        case-insensitive and reuses an existing case-variant tag row as-is, so the
        list just sent to ``setTags`` is never trustworthy; this is the only way to
        know what actually landed."""
        content_types = self._resolve_content_types()
        ct_id = content_types.get(table)
        if ct_id is None:
            return []
        rows = self._post(
            _TAGGED_ITEM_FOR_QUERY, {"content_type_id": ct_id, "object_id": object_id}
        )["taggedItem"]
        return [row["tag"]["name"] for row in rows]

    def update_report(self, report_id: int, fields: dict) -> None:
        """Patch a report's ``_set`` columns (grison only ever sends ``extraFields``)."""
        self._post(_UPDATE_REPORT_MUTATION, {"id": report_id, "set": fields})

    def download_evidence(self, evidence_id: int) -> tuple[str, bytes]:
        data = self._post(_DOWNLOAD_EVIDENCE_QUERY, {"id": evidence_id})["downloadEvidence"]
        raw = base64.b64decode(data["fileBase64"])
        return data["filename"], raw

    def insert_finding(self, fields: dict) -> dict:
        """Returns the full post-insert row (see ``_INSERT_FINDING_MUTATION``) —
        callers read ``["id"]`` for the new pk and pass the whole row straight into
        :func:`~grison.remote.gwmap.gw_record_to_finding` to build the canonical
        local finding without a second round trip."""
        data = self._post(_INSERT_FINDING_MUTATION, {"obj": fields})
        return data["insert_finding_one"]

    def update_finding(self, finding_id: int, fields: dict) -> dict:
        """Returns the full post-write row (see ``_UPDATE_FINDING_MUTATION``) — the
        value the next fetch would rebuild, so the caller can re-canonicalize from it
        instead of stamping the merge base from the pre-write local object."""
        data = self._post(_UPDATE_FINDING_MUTATION, {"id": finding_id, "set": fields})
        return data["update_finding_by_pk"]

    def insert_reported_finding(self, fields: dict) -> dict:
        data = self._post(_INSERT_REPORTED_FINDING_MUTATION, {"obj": fields})
        return data["insert_reportedFinding_one"]

    def update_reported_finding(self, reported_finding_id: int, fields: dict) -> dict:
        data = self._post(
            _UPDATE_REPORTED_FINDING_MUTATION,
            {"id": reported_finding_id, "set": fields},
        )
        return data["update_reportedFinding_by_pk"]

    def upload_evidence(
        self,
        *,
        finding_id: int,
        filename: str,
        caption: str,
        friendly_name: str,
        file_base64: str,
        description: str = "",
    ) -> int:
        data = self._post(
            _UPLOAD_EVIDENCE_MUTATION,
            {
                "finding": finding_id,
                "file_base64": file_base64,
                "filename": filename,
                "caption": caption,
                "friendly_name": friendly_name,
                "description": description,
            },
        )
        return data["uploadEvidence"]["id"]

    def update_evidence(self, evidence_id: int, fields: dict) -> dict:
        """Patch an evidence row's ``caption``/``friendlyName``/``description`` in place
        (Track 1b) — the update-permission columns confirmed live on ``evidence``.
        Returns the post-write row so the caller stamps its per-image merge base from
        what Ghostwriter actually stored, not the local strings that were sent.

        CAUTION (upstream, do not fight client-side): a ``friendlyName`` update fires a
        Ghostwriter-side event trigger that rewrites ``{{.Name}}``/``{{ref .Name}}``
        template references across every finding in the report. That's intended
        upstream behavior — grison just sends the new value and lets it happen.
        """
        data = self._post(_UPDATE_EVIDENCE_MUTATION, {"id": evidence_id, "set": fields})
        return data["update_evidence_by_pk"]

    def fetch_evidence_by_ids(self, ids: list[int]) -> list[dict]:
        """``id/document/caption/friendlyName/description`` rows for a set of evidence
        ids — used right after an upload batch to adopt Ghostwriter's stored basename
        (Django's storage appends ``_<rand7>`` on a filename collision, so the sent
        name isn't guaranteed to be what actually landed)."""
        if not ids:
            return []
        return self._post(_EVIDENCE_BY_IDS_QUERY, {"ids": ids})["evidence"]

    def delete_evidence(self, evidence_id: int) -> None:
        self._post(_DELETE_EVIDENCE_MUTATION, {"id": evidence_id})

    def delete_finding(self, finding_id: int) -> None:
        """Delete a library finding (used to roll back an insert)."""
        self._post(_DELETE_FINDING_MUTATION, {"id": finding_id})

    def delete_reported_finding(self, reported_finding_id: int) -> None:
        """Delete a report finding (used to roll back an insert)."""
        self._post(_DELETE_REPORTED_FINDING_MUTATION, {"id": reported_finding_id})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GhostwriterClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
