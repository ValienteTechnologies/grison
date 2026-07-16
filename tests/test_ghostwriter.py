"""Tests for the read-only Ghostwriter GraphQL client.

All requests go through ``httpx.MockTransport`` — no live Ghostwriter calls.
"""

from __future__ import annotations

import base64
import json
from datetime import date

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


_FINDING_SEVERITY_ROWS = [
    {"id": 1, "severity": "Informational", "weight": 1},
    {"id": 2, "severity": "Low", "weight": 2},
]
_FINDING_TYPE_LOOKUP_ROWS = [
    {"id": 1, "findingType": "Network"},
    {"id": 2, "findingType": "Physical"},
]


def test_fetch_finding_severities_parses_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _gql_response({"findingSeverity": _FINDING_SEVERITY_ROWS})

    with GhostwriterClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        rows = client.fetch_finding_severities()
    assert rows == _FINDING_SEVERITY_ROWS


def test_fetch_finding_types_parses_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _gql_response({"findingType": _FINDING_TYPE_LOOKUP_ROWS})

    with GhostwriterClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        rows = client.fetch_finding_types()
    assert rows == _FINDING_TYPE_LOOKUP_ROWS


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
        row = client.insert_finding(fields)
    assert row["id"] == 42
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
    assert result == {"id": 1}
    assert len(captured) == 1


def test_insert_reported_finding_sends_mutation_and_returns_id() -> None:
    fields = {"title": "New finding", "reportId": 7, "severityId": 3}
    transport, captured = _single_mutation_transport(
        expected_query_marker="insert_reportedFinding_one",
        expected_variables={"obj": fields},
        response_data={"insert_reportedFinding_one": {"id": 183}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        row = client.insert_reported_finding(fields)
    assert row["id"] == 183
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
    assert result == {"id": 183}
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
            "description": "",  # default when the caller doesn't pass one (Track 1b)
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


def test_upload_evidence_sends_description_when_given() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="uploadEvidence",
        expected_variables={
            "finding": 183,
            "file_base64": "ZmZk",
            "filename": "shell.jpeg",
            "caption": "Reverse shell",
            "friendly_name": "reverse-shell",
            "description": "Shell obtained via CVE-2024-xxxx",
        },
        response_data={"uploadEvidence": {"id": 38}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        client.upload_evidence(
            finding_id=183,
            filename="shell.jpeg",
            caption="Reverse shell",
            friendly_name="reverse-shell",
            file_base64="ZmZk",
            description="Shell obtained via CVE-2024-xxxx",
        )
    assert len(captured) == 1


def test_update_evidence_sends_camelcase_friendly_name() -> None:
    """update_evidence's ``_set`` uses the wire's camelCase ``friendlyName`` — the
    Hasura auto-generated ``evidence_set_input`` type, not the snake_case argument
    name ``uploadEvidence`` (a hand-written custom mutation) happens to use."""
    transport, captured = _single_mutation_transport(
        expected_query_marker="update_evidence_by_pk",
        expected_variables={
            "id": 38,
            "set": {"caption": "new caption", "friendlyName": "new-name"},
        },
        response_data={"update_evidence_by_pk": {"id": 38}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        result = client.update_evidence(38, {"caption": "new caption", "friendlyName": "new-name"})
    assert result == {"id": 38}
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


# --- tags/CWE (Track 1a): djangoContentType / taggedItem / setTags -------------------------

_CONTENT_TYPE_ROWS = [
    {"id": 67, "model": "finding"},
    {"id": 66, "model": "reportfindinglink"},
]

_TAGGED_ITEM_ROWS = [
    {"content_type_id": 67, "object_id": 42, "tag": {"name": "CWE:79"}},
    {"content_type_id": 67, "object_id": 42, "tag": {"name": "recon"}},
    {"content_type_id": 66, "object_id": 183, "tag": {"name": "CWE:89"}},
]


def _make_tag_transport(
    captured_requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured_requests is not None:
            captured_requests.append(request)
        body = json.loads(request.content)
        query = body["query"]
        if "setTags" in query:
            return _gql_response({"setTags": None})
        if "djangoContentType" in query:
            return _gql_response({"djangoContentType": _CONTENT_TYPE_ROWS})
        if "taggedItem" in query:
            ids = set(body.get("variables", {}).get("content_type_ids", []))
            rows = [r for r in _TAGGED_ITEM_ROWS if r["content_type_id"] in ids]
            return _gql_response({"taggedItem": rows})
        raise AssertionError(f"unexpected query: {query}")

    return httpx.MockTransport(handler)


def test_fetch_tag_map_resolves_content_types_and_splits_by_table() -> None:
    with GhostwriterClient(_CREDS, transport=_make_tag_transport()) as client:
        tag_map = client.fetch_tag_map()
    assert tag_map[("finding", 42)] == ["CWE:79", "recon"]
    assert tag_map[("reportedFinding", 183)] == ["CWE:89"]


def test_fetch_tag_map_caches_content_type_resolution() -> None:
    captured: list[httpx.Request] = []
    with GhostwriterClient(_CREDS, transport=_make_tag_transport(captured)) as client:
        client.fetch_tag_map()
        client.fetch_tag_map()
    ct_queries = [r for r in captured if "djangoContentType" in json.loads(r.content)["query"]]
    assert len(ct_queries) == 1  # resolved once per client, then cached


def test_set_tags_sends_finding_model_string_for_library_table() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="setTags",
        expected_variables={"id": 42, "model": "finding", "tags": ["CWE:79", "recon"]},
        response_data={"setTags": None},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        result = client.set_tags(42, "finding", ["CWE:79", "recon"])
    assert result is None
    assert len(captured) == 1


def test_set_tags_sends_report_finding_link_model_string_for_instance_table() -> None:
    """model strings are Django keys used by the setTags action handler ('finding' /
    'report_finding_link'), distinct from djangoContentType.model ('reportfindinglink',
    no underscore) — the two must not be conflated."""
    transport, captured = _single_mutation_transport(
        expected_query_marker="setTags",
        expected_variables={"id": 183, "model": "report_finding_link", "tags": ["CWE:89"]},
        response_data={"setTags": None},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        client.set_tags(183, "reportedFinding", ["CWE:89"])
    assert len(captured) == 1


# --- project context / notes (Track: pull GW project + append-only project notes) ----


def test_fetch_reports_query_includes_project_context_fields() -> None:
    """The extended ``_REPORT_QUERY`` must actually ask for the new project relations —
    a wrong/missing sub-selection would silently degrade project.md to an empty mirror."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _gql_response({"report": _REPORT_ROWS})

    with GhostwriterClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        client.fetch_reports()
    query = json.loads(captured[0].content)["query"]
    for marker in (
        "codename", "collab_note", "scopes {", "objectives {", "targets {",
        "whitecards {", "comments {", "objectiveStatus {", "objectivePriority {",
    ):
        assert marker in query


def test_whoami_parses_data_whoami() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _gql_response({"whoami": {"username": "operator1", "role": "user", "expires": None}})

    with GhostwriterClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        who = client.whoami()
    assert who == {"username": "operator1", "role": "user", "expires": None}


def test_resolve_user_id_returns_id_for_known_username() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="user(where:",
        expected_variables={"username": "operator1"},
        response_data={"user": [{"id": 42}]},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        user_id = client.resolve_user_id("operator1")
    assert user_id == 42
    assert len(captured) == 1


def test_resolve_user_id_returns_none_for_unknown_username() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _gql_response({"user": []})

    with GhostwriterClient(_CREDS, transport=httpx.MockTransport(handler)) as client:
        user_id = client.resolve_user_id("nobody")
    assert user_id is None


def test_insert_project_note_sends_mutation_and_returns_id() -> None:
    transport, captured = _single_mutation_transport(
        expected_query_marker="insert_projectNote_one",
        expected_variables={
            "obj": {
                "projectId": 1,
                "note": "<p>hello</p>",
                "operatorId": 42,
                "timestamp": "2026-07-16",
            }
        },
        response_data={"insert_projectNote_one": {"id": 100}},
    )
    with GhostwriterClient(_CREDS, transport=transport) as client:
        note_id = client.insert_project_note(1, "<p>hello</p>", 42, date(2026, 7, 16))
    assert note_id == 100
    assert len(captured) == 1
