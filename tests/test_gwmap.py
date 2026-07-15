"""Tests for the Ghostwriter record → Finding mapping + content-hash merge base.

Synthetic GW-shaped records (the real corpus is client data, verified offline during
the build, never committed)."""

from __future__ import annotations

from grison.markdown import finding_to_markdown, markdown_to_finding
from grison.model import FindingType, Severity
from grison.remote.gwmap import (
    clean_gw_html,
    content_hash,
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
