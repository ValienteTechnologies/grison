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

_INSERT_FINDING_MUTATION = """
mutation($obj: finding_insert_input!) {
  insert_finding_one(object: $obj) {
    id
  }
}
"""

_UPDATE_FINDING_MUTATION = """
mutation($id: bigint!, $set: finding_set_input!) {
  update_finding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
  }
}
"""

_INSERT_REPORTED_FINDING_MUTATION = """
mutation($obj: reportedFinding_insert_input!) {
  insert_reportedFinding_one(object: $obj) {
    id
  }
}
"""

_UPDATE_REPORTED_FINDING_MUTATION = """
mutation($id: bigint!, $set: reportedFinding_set_input!) {
  update_reportedFinding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
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
) {
  uploadEvidence(
    finding: $finding
    file_base64: $file_base64
    filename: $filename
    caption: $caption
    friendly_name: $friendly_name
  ) {
    id
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

    def update_report(self, report_id: int, fields: dict) -> None:
        """Patch a report's ``_set`` columns (grison only ever sends ``extraFields``)."""
        self._post(_UPDATE_REPORT_MUTATION, {"id": report_id, "set": fields})

    def download_evidence(self, evidence_id: int) -> tuple[str, bytes]:
        data = self._post(_DOWNLOAD_EVIDENCE_QUERY, {"id": evidence_id})["downloadEvidence"]
        raw = base64.b64decode(data["fileBase64"])
        return data["filename"], raw

    def insert_finding(self, fields: dict) -> int:
        data = self._post(_INSERT_FINDING_MUTATION, {"obj": fields})
        return data["insert_finding_one"]["id"]

    def update_finding(self, finding_id: int, fields: dict) -> None:
        self._post(_UPDATE_FINDING_MUTATION, {"id": finding_id, "set": fields})

    def insert_reported_finding(self, fields: dict) -> int:
        data = self._post(_INSERT_REPORTED_FINDING_MUTATION, {"obj": fields})
        return data["insert_reportedFinding_one"]["id"]

    def update_reported_finding(self, reported_finding_id: int, fields: dict) -> None:
        self._post(
            _UPDATE_REPORTED_FINDING_MUTATION,
            {"id": reported_finding_id, "set": fields},
        )

    def upload_evidence(
        self,
        *,
        finding_id: int,
        filename: str,
        caption: str,
        friendly_name: str,
        file_base64: str,
    ) -> int:
        data = self._post(
            _UPLOAD_EVIDENCE_MUTATION,
            {
                "finding": finding_id,
                "file_base64": file_base64,
                "filename": filename,
                "caption": caption,
                "friendly_name": friendly_name,
            },
        )
        return data["uploadEvidence"]["id"]

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
