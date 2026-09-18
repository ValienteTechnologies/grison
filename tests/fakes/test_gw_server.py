"""Unit tests for the fake Ghostwriter GraphQL server itself — schema validation,
Hasura-shaped errors, the store's CRUD/constraint behavior, and failure injection.

The "fidelity" tests load real captured responses from a Ghostwriter 7.2.6 lab
instance (``tests/fixtures/lab-samples/gw-*.json`` — synthetic data, see
``lab/LAB.md`` in the rework repo) and assert the fake reproduces the same row shape
byte-for-byte when seeded with the same data and queried the same way.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from grison.model.enums import FindingType, Severity
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient, GhostwriterError
from tests.fakes.gw_server import FakeGhostwriter, GhostwriterFakeError, load_schema

LAB_SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "lab-samples"


def _creds(gw: FakeGhostwriter) -> Creds:
    return Creds(gw_url="https://fake-gw.invalid", gw_token=gw.token)


def _client(gw: FakeGhostwriter) -> GhostwriterClient:
    # sleep=lambda: no-op so a retry test doesn't actually wait out the backoff
    return GhostwriterClient(_creds(gw), transport=gw.transport, sleep=lambda _: None)


def test_schema_loads_and_is_cached() -> None:
    s1 = load_schema()
    s2 = load_schema()
    assert s1 is s2
    assert s1.query_type is not None
    assert s1.query_type.name == "query_root"


def test_seed_defaults_match_grisons_own_enum_maps() -> None:
    gw = FakeGhostwriter()
    by_id = {row["id"]: row["severity"] for row in gw.store.finding_severities}
    for sev in Severity:
        assert by_id[sev.gw_id].lower() == sev.value
    by_id_ft = {row["id"]: row["findingType"] for row in gw.store.finding_types}
    for ft in FindingType:
        assert by_id_ft[ft.gw_id].lower() == ft.value


def test_evidence_query_no_longer_asks_for_findingId() -> None:
    """Was ``test_evidence_query_rejected_like_the_real_lab_server`` — confirmed live
    against Ghostwriter 7.2.6 (SSL_CERT_FILE=lab/lab-ca.pem) that a query asking
    ``evidence { findingId }`` gets ``field 'findingId' not found in type: 'evidence'``
    (D1: evidence belongs to a report, never a finding — the real schema has no such
    field at all). ``fetch_evidence`` no longer asks for it, so this is now a proof
    the fix stays fixed: fetching evidence succeeds against the schema-typed fake
    (which validates every query against the real 7.2.6 SDL) instead of raising."""
    gw = FakeGhostwriter()
    c = _client(gw)
    report = gw.store.seed_report(id=1, title="R")
    gw.store.seed_evidence(id=1, reportId=report["id"], friendlyName="shot", document="shot.png")
    rows = c.fetch_evidence()
    assert [r["id"] for r in rows] == [1]
    assert "findingId" not in rows[0]


def test_upload_evidence_uses_report_argument_not_finding() -> None:
    """Was ``test_upload_evidence_argument_error_matches_the_real_lab_server`` —
    confirmed live that ``uploadEvidence(finding: ...)`` gets ``'uploadEvidence' has
    no argument named 'finding'`` (the real argument is ``report``). Now a proof the
    fix stays fixed: uploading with ``report_id=`` succeeds."""
    gw = FakeGhostwriter()
    c = _client(gw)
    report = gw.store.seed_report(id=1, title="R")
    evidence_id = c.upload_evidence(
        report_id=report["id"], filename="x.png", caption="c", friendly_name="f",
        file_base64=base64.b64encode(b"x").decode(),
    )
    assert evidence_id > 0


def test_unknown_field_error_shape_generalizes() -> None:
    gw = FakeGhostwriter()
    resp = gw.handle(
        httpx.Request(
            "POST", "https://fake-gw.invalid/v1/graphql",
            headers={"Authorization": f"Bearer {gw.token}"},
            json={"query": "query { nonexistentField { id } }"},
        )
    )
    body = json.loads(resp.content)
    assert body == {
        "errors": [
            {
                "message": "field 'nonexistentField' not found in type: 'query_root'",
                "extensions": {"code": "validation-failed"},
            }
        ]
    }


def test_bad_bearer_token_matches_the_real_lab_server() -> None:
    gw = FakeGhostwriter(token="right-token")
    c = GhostwriterClient(
        Creds(gw_url="https://fake-gw.invalid", gw_token="wrong-token"), transport=gw.transport
    )
    with pytest.raises(GhostwriterError, match="Authentication hook unauthorized this request"):
        c.whoami()


# --- fidelity: real lab-captured shapes ---------------------------------------


def test_fidelity_finding_row_matches_lab_capture() -> None:
    sample = json.loads((LAB_SAMPLES / "gw-findings.json").read_text())["data"]["finding"][0]
    gw = FakeGhostwriter()
    gw.store.seed_finding(**sample)

    got = _client(gw).fetch_findings()[0]

    assert got == sample


def test_fidelity_evidence_row_matches_lab_capture() -> None:
    sample = json.loads((LAB_SAMPLES / "gw-evidence.json").read_text())["data"]["evidence"][0]
    gw = FakeGhostwriter()
    gw.store.seed_report(id=sample["reportId"])
    gw.store.seed_evidence(**sample)

    resp = gw.handle(
        httpx.Request(
            "POST", "https://fake-gw.invalid/v1/graphql",
            headers={"Authorization": f"Bearer {gw.token}"},
            json={
                "query": "query { evidence { id reportId document caption friendlyName "
                "description uploadDate } }"
            },
        )
    )
    got = json.loads(resp.content)["data"]["evidence"][0]
    assert got == sample


def test_fidelity_report_row_matches_lab_capture() -> None:
    sample = json.loads((LAB_SAMPLES / "gw-report.json").read_text())["data"]["report"][0]
    gw = FakeGhostwriter()
    gw.store.seed_report(**sample)

    got = _client(gw).fetch_reports()[0]

    assert got == sample


def test_fidelity_extra_field_spec_rows_match_lab_capture() -> None:
    """Captured live 2026-09-18 against the reset lab (report task E) —
    ``seed_defaults()``'s own 7-field default (ids 3-9, same internalName/position
    order) is this exact live shape, not a guess."""
    sample = json.loads(
        (LAB_SAMPLES / "gw-extra-field-spec.json").read_text()
    )["data"]["extraFieldSpec"]
    gw = FakeGhostwriter()  # seed_defaults() already seeds the 7-field default

    got = _client(gw).fetch_report_extra_field_specs()

    assert got == sample


# --- store CRUD + constraints -------------------------------------------------


def test_where_eq_in_and_combinators() -> None:
    gw = FakeGhostwriter()
    gw.store.seed_finding(id=1, severityId=5)
    gw.store.seed_finding(id=2, severityId=3)
    gw.store.seed_finding(id=3, severityId=5)

    resp = gw.handle(
        httpx.Request(
            "POST", "https://fake-gw.invalid/v1/graphql",
            headers={"Authorization": f"Bearer {gw.token}"},
            json={
                "query": "query($ids: [bigint!]) { finding(where: {_and: ["
                "{severityId: {_eq: 5}}, {id: {_in: $ids}}]}) { id } }",
                "variables": {"ids": [1, 2, 3]},
            },
        )
    )
    got = {row["id"] for row in json.loads(resp.content)["data"]["finding"]}
    assert got == {1, 3}


def test_insert_update_delete_finding_roundtrip() -> None:
    gw = FakeGhostwriter()
    c = _client(gw)
    fields = {
        "title": "New", "severityId": 3, "findingTypeId": 4, "cvssVector": "", "cvssScore": None,
        "description": "", "impact": "", "mitigation": "", "references": "",
        "replication_steps": "",
    }
    created = c.insert_finding(fields)
    assert created["title"] == "New"

    updated = c.update_finding(created["id"], {"title": "Renamed"})
    assert updated["title"] == "Renamed"
    assert gw.store.findings[0]["title"] == "Renamed"

    c.delete_finding(created["id"])
    assert gw.store.findings == []


def test_upload_evidence_requires_a_valid_report() -> None:
    gw = FakeGhostwriter()
    with pytest.raises(GhostwriterFakeError, match="does not exist"):
        gw._upload_evidence(
            report=999, filename="x.png", caption="", friendly_name="f",
            file_base64=base64.b64encode(b"x").decode(),
        )


def test_upload_evidence_friendly_name_unique_per_report() -> None:
    gw = FakeGhostwriter()
    gw.store.seed_report(id=1)
    gw._upload_evidence(
        report=1, filename="a.png", caption="", friendly_name="dup",
        file_base64=base64.b64encode(b"a").decode(),
    )
    with pytest.raises(GhostwriterFakeError, match="Uniqueness violation"):
        gw._upload_evidence(
            report=1, filename="b.png", caption="", friendly_name="dup",
            file_base64=base64.b64encode(b"b").decode(),
        )


def test_deleting_a_reported_finding_does_not_delete_its_reports_evidence() -> None:
    gw = FakeGhostwriter()
    gw.store.seed_report(id=1)
    gw.store.seed_reported_finding(id=5, reportId=1)
    gw.store.seed_evidence(id=90, reportId=1, friendlyName="shot")

    c = _client(gw)
    c.delete_reported_finding(5)

    assert gw.store.reported_findings == []
    assert len(gw.store.evidence) == 1  # untouched — evidence belongs to the report, not a finding


def test_set_tags_is_replace_all() -> None:
    gw = FakeGhostwriter()
    c = _client(gw)
    c.set_tags(1, "finding", ["CWE:79", "a"])
    assert c.fetch_tags_for("finding", 1) == ["CWE:79", "a"]
    c.set_tags(1, "finding", ["b"])
    assert c.fetch_tags_for("finding", 1) == ["b"]


def test_download_evidence_roundtrip() -> None:
    gw = FakeGhostwriter()
    report = gw.store.seed_report(id=1)
    ev = gw.store.seed_evidence(reportId=report["id"], document="evidence/1/x.png", content=b"hi")
    c = _client(gw)
    filename, data = c.download_evidence(ev["id"])
    assert filename == "x.png"
    assert data == b"hi"


# --- failure injection ---------------------------------------------------------


def test_http_500_injection() -> None:
    gw = FakeGhostwriter()
    gw.inject_http_500(times=1)
    c = _client(gw)
    with pytest.raises(GhostwriterError, match="HTTP 500"):
        c.whoami()
    c.whoami()  # the injection was one-shot — this call succeeds


def test_timeout_injection_on_a_query_is_retried_and_succeeds() -> None:
    """Behavior change (deliberate, grison.remote.http): a query is idempotent, so
    a one-shot transient timeout is retried transparently instead of raising —
    this used to raise before the retry policy landed."""
    gw = FakeGhostwriter()
    gw.inject_timeout(times=1)
    c = _client(gw)
    result = c.whoami()  # does NOT raise — the retry absorbs the one-shot timeout
    assert result["username"]


def test_timeout_injection_still_raises_once_attempts_are_exhausted() -> None:
    gw = FakeGhostwriter()
    gw.inject_timeout(times=10)  # far more than max_attempts
    c = GhostwriterClient(_creds(gw), transport=gw.transport, sleep=lambda _: None,
                           max_attempts=3)
    with pytest.raises(httpx.TimeoutException):
        c.whoami()


def test_graphql_error_injection_targets_one_operation() -> None:
    gw = FakeGhostwriter()
    gw.inject_graphql_error("whoami", "simulated outage", times=1)
    c = _client(gw)
    with pytest.raises(GhostwriterError, match="simulated outage"):
        c.whoami()
    c.whoami()  # untargeted operation, and the injection was one-shot: unaffected


def test_operation_log_records_mutations_in_order_with_variables() -> None:
    gw = FakeGhostwriter()
    c = _client(gw)
    c.insert_finding({
        "title": "A", "severityId": 3, "findingTypeId": 4, "cvssVector": "", "cvssScore": None,
        "description": "", "impact": "", "mitigation": "", "references": "",
        "replication_steps": "",
    })
    c.set_tags(1000, "finding", ["x"])
    names = [op.name for op in gw.operation_log]
    assert names == ["insert_finding_one", "setTags"]
    assert gw.operation_log[1].variables == {"id": 1000, "model": "finding", "tags": ["x"]}


def test_on_request_hook_fires_before_the_nth_call_is_resolved() -> None:
    gw = FakeGhostwriter()
    gw.store.seed_report(id=1, title="R")
    c = _client(gw)

    def mutate() -> None:
        gw.store.reports[0]["title"] = "Mutated"

    gw.on_request("report", mutate, call_number=2)
    first = c.fetch_reports()[0]["title"]
    second = c.fetch_reports()[0]["title"]

    assert first == "R"  # untouched before the hook's target call
    assert second == "Mutated"  # mutated immediately before the 2nd call resolved


def test_request_log_records_every_operation_not_just_mutations() -> None:
    gw = FakeGhostwriter()
    gw.store.seed_finding(id=1)
    c = _client(gw)
    c.fetch_findings()
    c.set_tags(1, "finding", ["x"])

    names = [r.name for r in gw.request_log]
    assert names == ["finding", "setTags"]
    assert gw.call_count("finding") == 1
    assert gw.call_count("setTags") == 1
    assert gw.call_count("nonexistent-op") == 0
