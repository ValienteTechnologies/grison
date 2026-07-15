"""Tests for the Ghostwriter record → Finding mapping + content-hash merge base.

Synthetic GW-shaped records (the real corpus is client data, verified offline during
the build, never committed)."""

from __future__ import annotations

from grison.markdown import finding_to_markdown, markdown_to_finding
from grison.model import FindingType, Severity
from grison.remote.gwmap import (
    clean_gw_html,
    content_hash,
    finding_to_gw_tags,
    gw_record_to_finding,
    stamp_synced,
)

_LIBRARY = {
    "id": 42,
    "title": "  Weak TLS ciphers  ",  # GW data sometimes has stray whitespace
    "severityId": 3,  # medium
    "findingTypeId": 4,  # web
    "cvssVector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "cvssScore": 5.3,
    "description": "<p>Uses <strong>deprecated</strong> ciphers.</p>",
    "impact": "<ul><li><p>MITM exposure</p></li></ul>",
    "mitigation": "<p>Disable TLS 1.0/1.1.</p>",
    "references": "",
    "replication_steps": "",
}

_INSTANCE = {
    **_LIBRARY,
    "id": 183,
    "reportId": 7,
    "affectedEntities": "<p>192.0.2.10</p><p>192.0.2.11</p>",
}

_EVIDENCE = [
    {
        "id": 38,
        "findingId": 183,
        "reportId": None,
        "document": "evidence/4/shell.jpeg",
        "caption": "Reverse shell",
        "friendlyName": "reverse-shell",
    }
]


def test_library_record_maps() -> None:
    f = gw_record_to_finding(_LIBRARY, tier="library")
    assert f.grison.tier == "library"
    assert f.grison.gw.table == "finding" and f.grison.gw.id == 42
    assert f.title == "Weak TLS ciphers"  # stripped
    assert f.severity is Severity.MEDIUM and f.finding_type is FindingType.WEB
    assert f.cvss is not None and f.cvss.score == 5.3
    assert f.impact == "- MITM exposure"
    assert f.affected_entities is None and f.evidence == []


def test_instance_record_with_evidence_maps() -> None:
    f = gw_record_to_finding(_INSTANCE, tier="instance", evidence_rows=_EVIDENCE)
    assert f.grison.gw.table == "reportedFinding"
    assert f.grison.gw.report_id == 7
    assert f.affected_entities == "192.0.2.10\n\n192.0.2.11"
    assert len(f.evidence) == 1
    ev = f.evidence[0]
    assert ev.file == "evidence/shell.jpeg"  # basename of the internal document path
    assert ev.friendly_name == "reverse-shell"
    assert ev.gw is not None and ev.gw.id == 38


def test_mapped_record_roundtrips_through_document() -> None:
    f = gw_record_to_finding(_INSTANCE, tier="instance", evidence_rows=_EVIDENCE)
    assert markdown_to_finding(finding_to_markdown(f)) == f


def test_clean_gw_html_headings_to_paragraphs() -> None:
    assert clean_gw_html("<h3>Notes</h3><p>x</p>") == "<p>Notes</p><p>x</p>"


def test_content_hash_excludes_sync_metadata() -> None:
    f = gw_record_to_finding(_LIBRARY, tier="library")
    h1 = content_hash(f)
    stamp_synced(f)  # sets synced.hash + synced.at
    assert f.grison.synced is not None
    assert f.grison.synced.hash == h1  # stamping did not change the content hash
    assert content_hash(f) == h1  # and recomputing is stable


def test_content_hash_changes_with_content() -> None:
    a = gw_record_to_finding(_LIBRARY, tier="library")
    b = gw_record_to_finding({**_LIBRARY, "title": "Different"}, tier="library")
    assert content_hash(a) != content_hash(b)


# --- tags/CWE (Track 1a) ---------------------------------------------------------------


def test_gw_record_to_finding_splits_known_cwe_tags_from_the_rest() -> None:
    f = gw_record_to_finding(
        _LIBRARY, tier="library",
        tags=["CWE:79", "ATT&CK:T1190", "CWE:", "CWE:319 ATT&CK:TA0006 ATT&CK:T1040"],
    )
    assert f.cwe == ["CWE-79"]  # known CWE, normalized
    # junk/unparseable "CWE:"-shaped names never invented into cwe — stay verbatim, in order
    assert f.tags == ["ATT&CK:T1190", "CWE:", "CWE:319 ATT&CK:TA0006 ATT&CK:T1040"]


def test_gw_record_to_finding_normalizes_colon_dash_and_space_cwe_variants() -> None:
    f = gw_record_to_finding(_LIBRARY, tier="library", tags=["CWE-79", "cwe: 79", "CWE:  79"])
    assert f.cwe == ["CWE-79", "CWE-79", "CWE-79"]
    assert f.tags == []


def test_gw_record_to_finding_unknown_cwe_id_stays_a_verbatim_tag() -> None:
    # a CWE-shaped name whose number isn't in the embedded index must not be invented
    f = gw_record_to_finding(_LIBRARY, tier="library", tags=["CWE:999999999"])
    assert f.cwe == []
    assert f.tags == ["CWE:999999999"]


def test_finding_to_gw_tags_projects_cwe_before_tags() -> None:
    f = gw_record_to_finding(_LIBRARY, tier="library", tags=["CWE:79", "recon"])
    assert finding_to_gw_tags(f) == ["CWE:79", "recon"]


def test_content_hash_sensitive_to_cwe_and_tags() -> None:
    a = gw_record_to_finding(_LIBRARY, tier="library")
    b = gw_record_to_finding(_LIBRARY, tier="library", tags=["CWE:79"])
    c = gw_record_to_finding(_LIBRARY, tier="library", tags=["recon"])
    assert content_hash(a) != content_hash(b) != content_hash(c)


def test_content_hash_ignores_cwe_tags_reorder() -> None:
    a = gw_record_to_finding(_LIBRARY, tier="library", tags=["CWE:79", "alpha", "beta"])
    b = gw_record_to_finding(_LIBRARY, tier="library", tags=["beta", "alpha", "CWE:79"])
    assert content_hash(a) == content_hash(b)
