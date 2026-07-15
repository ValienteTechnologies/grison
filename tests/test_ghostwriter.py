"""Tests for the read-only Ghostwriter GraphQL client.

All requests go through ``httpx.MockTransport`` — no live Ghostwriter calls.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient, GhostwriterError

_CREDS = Creds(
    gw_url="https://gw.example",
    gw_token="t",
    cf_client_id="cid",
    cf_client_secret="sec",
)

_FINDING_ROWS = [
    {
        "id": 1,
        "title": "Weak TLS ciphers",
        "severityId": 3,
        "findingTypeId": 4,
        "cvssScore": 5.3,
        "cvssVector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
        "description": "<p>desc</p>",
        "impact": "<p>impact</p>",
        "mitigation": "<p>fix</p>",
        "references": "",
        "replication_steps": "",
    },
    {
        "id": 2,
        "title": "Another finding",
        "severityId": 2,
        "findingTypeId": 1,
        "cvssScore": None,
        "cvssVector": "",
        "description": "",
        "impact": "",
        "mitigation": "",
        "references": "",
        "replication_steps": "",
    },
]

_REPORTED_FINDING_ROWS = [
    {
        "id": 183,
        "reportId": 7,
        "title": "Weak TLS ciphers",
        "severityId": 3,
        "findingTypeId": 4,
        "cvssScore": 5.3,
        "cvssVector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
        "description": "<p>desc</p>",
        "impact": "<p>impact</p>",
        "mitigation": "<p>fix</p>",
        "references": "",
        "replication_steps": "",
        "affectedEntities": "<p>192.0.2.10</p>",
    }
]

_EVIDENCE_ROWS = [
    {
        "id": 38,
        "findingId": 183,
        "reportId": None,
        "document": "evidence/4/shell.jpeg",
        "caption": "Reverse shell",
        "friendlyName": "reverse-shell",
    }
]

_REPORT_ROWS = [{"id": 7, "title": "Q3 Pentest"}]

_EVIDENCE_BYTES = b"\xff\xd8\xff\xe0JFIF"


def _gql_response(data: dict, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"data": data})


def _make_transport(captured_requests: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured_requests is not None:
            captured_requests.append(request)
        body = json.loads(request.content)
        query = body["query"]
        # Ordered by specificity: "findingTypeId"/"findingId" make plain substring
        # checks like "finding" in query match multiple queries, so match on the
        # top-level field markers instead (e.g. "finding {" vs "reportedFinding {").
        if "downloadEvidence" in query:
            variables = body.get("variables", {})
            assert variables == {"id": 38}
            return _gql_response(
                {
                    "downloadEvidence": {
                        "fileBase64": base64.b64encode(_EVIDENCE_BYTES).decode(),
                        "filename": "shell.jpeg",
                    }
                }
            )
        if "evidence {" in query:
            return _gql_response({"evidence": _EVIDENCE_ROWS})
        if "reportedFinding {" in query:
            return _gql_response({"reportedFinding": _REPORTED_FINDING_ROWS})
        if "report {" in query:
            return _gql_response({"report": _REPORT_ROWS})
        if "finding {" in query:
            return _gql_response({"finding": _FINDING_ROWS})
        raise AssertionError(f"unexpected query: {query}")

    return httpx.MockTransport(handler)


def test_fetch_findings_parses_data_finding() -> None:
    with GhostwriterClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_findings()
    assert rows == _FINDING_ROWS


def test_fetch_reported_findings_parses_data_reported_finding() -> None:
    with GhostwriterClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_reported_findings()
    assert rows == _REPORTED_FINDING_ROWS


def test_fetch_evidence_parses_data_evidence() -> None:
    with GhostwriterClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_evidence()
    assert rows == _EVIDENCE_ROWS


def test_fetch_reports_parses_data_report() -> None:
    with GhostwriterClient(_CREDS, transport=_make_transport()) as client:
        rows = client.fetch_reports()
    assert rows == _REPORT_ROWS


def test_requests_carry_auth_and_cf_headers() -> None:
    captured: list[httpx.Request] = []
    with GhostwriterClient(_CREDS, transport=_make_transport(captured)) as client:
        client.fetch_findings()
        client.fetch_reports()
    assert captured  # sanity: requests were actually made
    for req in captured:
        assert req.headers["Authorization"] == "Bearer t"
        assert req.headers["CF-Access-Client-Id"] == "cid"
        assert req.headers["CF-Access-Client-Secret"] == "sec"


def test_download_evidence_decodes_base64_and_returns_filename() -> None:
    with GhostwriterClient(_CREDS, transport=_make_transport()) as client:
        filename, raw = client.download_evidence(38)
    assert filename == "shell.jpeg"
    assert raw == _EVIDENCE_BYTES


def test_graphql_errors_raise_ghostwriter_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    transport = httpx.MockTransport(handler)
    with GhostwriterClient(_CREDS, transport=transport) as client:
        with pytest.raises(GhostwriterError, match="boom"):
            client.fetch_findings()


def test_http_error_status_raises_ghostwriter_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    transport = httpx.MockTransport(handler)
    with GhostwriterClient(_CREDS, transport=transport) as client:
        with pytest.raises(GhostwriterError):
            client.fetch_findings()


def _single_mutation_transport(
    *, expected_query_marker: str, expected_variables: dict, response_data: dict
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        assert expected_query_marker in body["query"]
        assert body["variables"] == expected_variables
        return _gql_response(response_data)

    return httpx.MockTransport(handler), captured


def test_insert_finding_sends_mutation_and_returns_id() -> None:
    fields = {"title": "New finding", "severityId": 3, "reportId": 7}
    transport, captured = _single_mutation_transport(
        expected_query_marker="insert_finding_one",
        expected_variables={"obj": fields},
        response_data={"insert_finding_one": {"id": 42}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        finding_id = client.insert_finding(fields)
    assert finding_id == 42
    assert len(captured) == 1


def test_update_finding_sends_mutation() -> None:
    fields = {"title": "Updated title"}
    transport, captured = _single_mutation_transport(
        expected_query_marker="update_finding_by_pk",
        expected_variables={"id": 1, "set": fields},
        response_data={"update_finding_by_pk": {"id": 1}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        result = client.update_finding(1, fields)
    assert result is None
    assert len(captured) == 1


def test_insert_reported_finding_sends_mutation_and_returns_id() -> None:
    fields = {"title": "New finding", "reportId": 7, "severityId": 3}
    transport, captured = _single_mutation_transport(
        expected_query_marker="insert_reportedFinding_one",
        expected_variables={"obj": fields},
        response_data={"insert_reportedFinding_one": {"id": 183}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        reported_finding_id = client.insert_reported_finding(fields)
    assert reported_finding_id == 183
    assert len(captured) == 1


def test_update_reported_finding_sends_mutation() -> None:
    fields = {"description": "<p>desc</p>"}
    transport, captured = _single_mutation_transport(
        expected_query_marker="update_reportedFinding_by_pk",
        expected_variables={"id": 183, "set": fields},
        response_data={"update_reportedFinding_by_pk": {"id": 183}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        result = client.update_reported_finding(183, fields)
    assert result is None
    assert len(captured) == 1


def test_upload_evidence_sends_mutation_and_returns_id() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="uploadEvidence",
        expected_variables={
            "finding": 183,
            "file_base64": "ZmZk",
            "filename": "shell.jpeg",
            "caption": "Reverse shell",
            "friendly_name": "reverse-shell",
        },
        response_data={"uploadEvidence": {"id": 38}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        evidence_id = client.upload_evidence(
            finding_id=183,
            filename="shell.jpeg",
            caption="Reverse shell",
            friendly_name="reverse-shell",
            file_base64="ZmZk",
        )
    assert evidence_id == 38
    assert len(captured) == 1


def test_delete_evidence_sends_mutation() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="delete_evidence_by_pk",
        expected_variables={"id": 38},
        response_data={"delete_evidence_by_pk": {"id": 38}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        result = client.delete_evidence(38)
    assert result is None
    assert len(captured) == 1


def test_mutation_graphql_errors_raise_ghostwriter_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "constraint violation"}]})

    transport = httpx.MockTransport(handler)
    with GhostwriterClient(_CREDS, transport=transport) as client:
        with pytest.raises(GhostwriterError, match="constraint violation"):
            client.insert_finding({"title": "x"})
