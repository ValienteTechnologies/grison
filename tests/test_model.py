"""Phase-2 model tests: enum ⇄ GW id, Finding validation as guardrail messages."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grison.model import Finding, FindingType, Severity


def test_severity_gw_id_roundtrip() -> None:
    # id order is the inverse of weight — 1=Informational … 5=Critical.
    assert Severity.INFORMATIONAL.gw_id == 1
    assert Severity.CRITICAL.gw_id == 5
    for s in Severity:
        assert Severity.from_gw_id(s.gw_id) is s
    with pytest.raises(ValueError, match="severityId"):
        Severity.from_gw_id(99)


def test_finding_type_gw_id_roundtrip() -> None:
    assert FindingType.NETWORK.gw_id == 1
    assert FindingType.HOST.gw_id == 7
    for t in FindingType:
        assert FindingType.from_gw_id(t.gw_id) is t
    with pytest.raises(ValueError, match="findingTypeId"):
        FindingType.from_gw_id(0)


def _library_finding(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "grison": {"tier": "library"},
        "severity": "medium",
        "finding_type": "web",
        "title": "Weak TLS ciphers",
    }
    base.update(over)
    return base


def test_library_finding_valid_and_table_derived() -> None:
    f = Finding.model_validate(_library_finding())
    assert f.grison.tier == "library"
    assert f.grison.gw.table == "finding"  # derived from tier
    assert f.severity is Severity.MEDIUM
    assert f.finding_type is FindingType.WEB


def test_instance_finding_table_derived_and_report_id() -> None:
    f = Finding.model_validate(
        {
            "grison": {"tier": "instance", "gw": {"id": 183, "report_id": 6}},
            "severity": "high",
            "finding_type": "web",
            "title": "SQLi",
            "affected_entities": "https://acme.example/",
        }
    )
    assert f.grison.gw.table == "reportedFinding"
    assert f.grison.gw.report_id == 6


def test_cvss_accepts_30_and_computes_score() -> None:
    f = Finding.model_validate(
        _library_finding(cvss={"vector": "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"})
    )
    assert f.cvss is not None
    assert f.cvss.score == 9.8  # computed from the vector


def test_cvss_rejects_wrong_version() -> None:
    with pytest.raises(ValidationError) as ei:
        Finding.model_validate(
            _library_finding(cvss={"vector": "CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P"})
        )
    assert "cvss" in str(ei.value).lower() or "vector" in str(ei.value).lower()


def test_cwe_hit_normalized_and_miss_rejected() -> None:
    f = Finding.model_validate(_library_finding(cwe=["16", "cwe-79"]))
    assert f.cwe == ["CWE-16", "CWE-79"]  # normalized to canonical form
    assert f.cwe_names["CWE-16"] == "Configuration"

    with pytest.raises(ValidationError, match="unknown CWE"):
        Finding.model_validate(_library_finding(cwe=["CWE-9999999"]))


def test_evidence_and_affected_entities_are_instance_only() -> None:
    with pytest.raises(ValidationError, match="instance-only"):
        Finding.model_validate(_library_finding(affected_entities="https://x/"))
    with pytest.raises(ValidationError, match="instance-only"):
        Finding.model_validate(
            _library_finding(evidence=[{"file": "evidence/x.png", "caption": "c"}])
        )


def test_tier_table_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="requires gw.table"):
        Finding.model_validate(
            {
                "grison": {"tier": "library", "gw": {"table": "reportedFinding"}},
                "severity": "low",
                "finding_type": "host",
                "title": "x",
            }
        )


def test_unknown_frontmatter_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(_library_finding(bogus_key="oops"))


def test_bad_enum_value_message_lists_options() -> None:
    with pytest.raises(ValidationError) as ei:
        Finding.model_validate(_library_finding(severity="sev-5"))
    assert "severity" in str(ei.value).lower()
