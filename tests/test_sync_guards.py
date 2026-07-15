"""Track 1c: sync-engine safety rails — corrupt-file guard (gw-pull F1), cross-report
move reparent (gw-pull F2), cvss score/hash consistency (F3/cvss-score-unhashed), and
the severity/finding-type drift tripwire. Mirrors tests/test_sync_push.py's FakeGW
idiom. No live calls."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from grison.markdown import finding_to_markdown, markdown_to_finding
from grison.model import EnumDriftError, Finding, parse_cvss
from grison.remote import snapshot as snapshot_mod
from grison.remote.gwmap import finding_to_gw_fields, stamp_synced
from grison.remote.sync import sync
from grison.sinks.file_sink import slugify


class FakeGW:
    def __init__(self) -> None:
        self.findings: dict[int, dict] = {}
        self.reported: dict[int, dict] = {}
        self.evidence: dict[int, dict] = {}
        self.images: dict[int, bytes] = {}
        self.tags: dict[tuple[str, int], list[str]] = {}
        self.reports = {2: {"id": 2, "title": "Acme"}}
        self._next = 1000

    def _id(self) -> int:
        self._next += 1
        return self._next

    def fetch_findings(self):
        return list(self.findings.values())

    def fetch_reported_findings(self):
        return list(self.reported.values())

    def fetch_evidence(self):
        return list(self.evidence.values())

    def fetch_reports(self):
        return list(self.reports.values())

    def fetch_tag_map(self):
        return dict(self.tags)

    def set_tags(self, record_id: int, table: str, tags: list[str]) -> None:
        self.tags[(table, record_id)] = list(tags)

    def fetch_finding_severities(self):
        return [
            {"id": 1, "severity": "Informational", "weight": 1},
            {"id": 2, "severity": "Low", "weight": 2},
            {"id": 3, "severity": "Medium", "weight": 3},
            {"id": 4, "severity": "High", "weight": 4},
            {"id": 5, "severity": "Critical", "weight": 5},
        ]

    def fetch_finding_types(self):
        return [
            {"id": 1, "findingType": "Network"},
            {"id": 2, "findingType": "Physical"},
            {"id": 3, "findingType": "Wireless"},
            {"id": 4, "findingType": "Web"},
            {"id": 5, "findingType": "Mobile"},
            {"id": 6, "findingType": "Cloud"},
            {"id": 7, "findingType": "Host"},
        ]

    def download_evidence(self, evidence_id: int):
        return (f"{evidence_id}.png", self.images[evidence_id])

    def insert_finding(self, fields: dict) -> int:
        i = self._id()
        self.findings[i] = {"id": i, **fields}
        return i

    def update_finding(self, finding_id: int, fields: dict) -> None:
        self.findings[finding_id] = {"id": finding_id, **fields}

    def insert_reported_finding(self, fields: dict) -> int:
        i = self._id()
        self.reported[i] = {"id": i, **fields}
        return i

    def update_reported_finding(self, rid: int, fields: dict) -> None:
        self.reported[rid] = {"id": rid, **fields}

    def upload_evidence(
        self, *, finding_id, filename, caption, friendly_name, file_base64, description=""
    ) -> int:
        i = self._id()
        self.images[i] = base64.b64decode(file_base64)
        self.evidence[i] = {
            "id": i, "findingId": finding_id, "reportId": None,
            "document": f"evidence/{filename}", "caption": caption, "friendlyName": friendly_name,
            "description": description,
        }
        return i

    def update_evidence(self, evidence_id: int, fields: dict) -> None:
        self.evidence[evidence_id].update(fields)

    def delete_evidence(self, evidence_id: int) -> None:
        self.evidence.pop(evidence_id, None)
        self.images.pop(evidence_id, None)

    def delete_finding(self, finding_id: int) -> None:
        self.findings.pop(finding_id, None)

    def delete_reported_finding(self, rid: int) -> None:
        self.reported.pop(rid, None)


def _finding(
    *, tier="library", gw_id=None, report_id=None, title="T", desc="body", **extra
) -> Finding:
    data: dict = {
        "grison": {"tier": tier, "gw": {"id": gw_id}},
        "severity": "medium", "finding_type": "web", "title": title, "description": desc,
    }
    if report_id is not None:
        data["grison"]["gw"]["report_id"] = report_id
    data.update(extra)
    return Finding.model_validate(data)


def _write(path: Path, f: Finding) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(finding_to_markdown(f))


def _seed_synced(
    root: Path, fake: FakeGW, *, tier="library", report_id=None, title="T", desc="body", **extra
):
    """Insert a remote record + write a matching, in-sync local file."""
    finding = _finding(tier=tier, report_id=report_id, title=title, desc=desc, **extra)
    fields = finding_to_gw_fields(finding)
    rid = fake.insert_finding(fields) if tier == "library" else fake.insert_reported_finding(fields)
    f = _finding(tier=tier, gw_id=rid, report_id=report_id, title=title, desc=desc, **extra)
    stamp_synced(f)
    if tier == "library":
        path = root / "findings" / "library" / f"{slugify(title)}.md"
    else:
        path = root / "findings" / "reports" / f"{report_id}-acme" / f"{rid}-{slugify(title)}.md"
    _write(path, f)
    return path, rid


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


# --- corrupt-file guard (gw-pull F1) --------------------------------------------------


def test_corrupt_file_identity_claimed_not_phantom_pulled(tmp_path: Path) -> None:
    """A single invalid frontmatter field (here: the required `severity` line dropped)
    must not let the remote-only pull loop re-materialize the record over the broken
    file — the file's own local edit (the new sentence) must survive untouched, and
    nothing may be written to Ghostwriter."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Broken Sev")
    before_remote = dict(fake.findings[rid])
    text = path.read_text()
    assert "severity: medium" in text
    broken = text.replace("severity: medium\n", "").replace(
        "body", "IMPORTANT NEW FINDING DETAIL the client asked us to add"
    )
    path.write_text(broken)

    r = sync(tmp_path, fake)

    assert any(p == path for p, _msg in r.corrupt)
    assert r.pulled == [] and r.pushed == [] and r.inserted == []
    assert fake.findings[rid] == before_remote  # remote completely untouched
    assert "IMPORTANT NEW FINDING DETAIL" in path.read_text()  # local edit preserved
    assert "severity:" not in path.read_text()  # still broken — not silently "fixed"


def test_corrupt_file_recovers_once_user_fixes_it(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Broken Sev Two")
    text = path.read_text()
    broken = text.replace("severity: medium\n", "").replace(
        "body", "IMPORTANT NEW FINDING DETAIL"
    )
    path.write_text(broken)
    r1 = sync(tmp_path, fake)
    assert any(p == path for p, _msg in r1.corrupt)

    fixed = broken.replace("finding_type: web", "finding_type: web\nseverity: medium")
    path.write_text(fixed)
    r2 = sync(tmp_path, fake)

    assert r2.corrupt == []
    assert path in r2.pushed
    assert "IMPORTANT NEW FINDING DETAIL" in fake.findings[rid]["description"]


# --- cross-report move reparent (gw-pull F2) ------------------------------------------


def test_cross_report_move_reparents_instead_of_duplicating(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.reports[3] = {"id": 3, "title": "OtherCo"}
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Movable")
    dest = tmp_path / "findings" / "reports" / "3-otherco" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    path.rename(dest)  # a genuine mv — the old file no longer exists anywhere

    r = sync(tmp_path, fake)

    assert dest in r.pushed
    assert dest not in r.inserted and r.inserted == []
    assert len(fake.reported) == 1  # one record, reparented — no duplicate
    assert fake.reported[rid]["reportId"] == 3
    f = markdown_to_finding(dest.read_text())
    assert f.grison.gw.id == rid and f.grison.gw.report_id == 3

    r2 = sync(tmp_path, fake)  # settles clean
    assert r2.unchanged == [dest]


def test_cross_report_copy_with_original_present_still_inserts(tmp_path: Path) -> None:
    """A COPY (the original file still lives at its home report) must not reparent the
    original — it stays the old insert-a-new-record behavior, since the original local
    file is still the legitimate claimant of that id."""
    fake = FakeGW()
    fake.reports[3] = {"id": 3, "title": "OtherCo"}
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Copyable")
    dest = tmp_path / "findings" / "reports" / "3-otherco" / "copy.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(path.read_text())  # cp, not mv

    r = sync(tmp_path, fake)

    assert dest in r.inserted
    assert len(fake.reported) == 2  # a genuinely new record; original untouched
    assert fake.reported[rid]["reportId"] == 2


# --- cvss score / vector consistency (F3, cvss-score-unhashed) -----------------------

_VEC_A = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"
_VEC_B = "CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"  # AV:N -> AV:L


def test_cvss_score_only_edit_pushes_recomputed_score_and_warns(tmp_path: Path) -> None:
    correct = parse_cvss(_VEC_A).base_score
    fake = FakeGW()
    path, rid = _seed_synced(
        tmp_path, fake, title="Score Edit", cvss={"vector": _VEC_A, "score": correct}
    )
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["cvss"]["score"] = correct + 1.0  # hand-edited to something inconsistent
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)

    assert path in r.pushed
    assert fake.findings[rid]["cvssScore"] == correct  # never the mathematically-wrong value
    assert any("cvss score" in w for w in r.warnings)
    f2 = markdown_to_finding(path.read_text())
    assert f2.cvss.score == correct  # local file corrected too


def test_remote_cvss_score_only_edit_pulls(tmp_path: Path) -> None:
    correct = parse_cvss(_VEC_A).base_score
    fake = FakeGW()
    path, rid = _seed_synced(
        tmp_path, fake, title="Remote Score Edit", cvss={"vector": _VEC_A, "score": correct}
    )
    fake.findings[rid]["cvssScore"] = correct - 1.0  # GW-side correction, vector untouched

    r = sync(tmp_path, fake)

    assert path in r.pulled
    f = markdown_to_finding(path.read_text())
    assert f.cvss.score == correct - 1.0


def test_cvss_vector_edit_with_stale_score_pushes_recomputed_and_warns(tmp_path: Path) -> None:
    score_a = parse_cvss(_VEC_A).base_score
    score_b = parse_cvss(_VEC_B).base_score
    assert score_a != score_b  # sanity: the edit actually changes the correct score
    fake = FakeGW()
    path, rid = _seed_synced(
        tmp_path, fake, title="Vector Edit", cvss={"vector": _VEC_A, "score": score_a}
    )
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["cvss"]["vector"] = _VEC_B  # leave the old score line as-is — now stale
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)

    assert path in r.pushed
    assert fake.findings[rid]["cvssVector"] == _VEC_B
    assert fake.findings[rid]["cvssScore"] == score_b  # recomputed, not the stale score_a
    assert any("cvss score" in w for w in r.warnings)
    f2 = markdown_to_finding(path.read_text())
    assert f2.cvss.score == score_b  # local file rewritten to match


# --- severity/finding-type drift tripwire ---------------------------------------------


def test_sync_ok_when_lookup_tables_match_the_hardcoded_maps(tmp_path: Path) -> None:
    fake = FakeGW()  # default fetch_finding_severities/types are consistent by construction
    r = sync(tmp_path, fake)  # must not raise
    assert r.errors == []


def test_sync_aborts_on_severity_table_drift(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.fetch_finding_severities = lambda: [
        {"id": 1, "severity": "Informational", "weight": 1},
        {"id": 2, "severity": "Low", "weight": 2},
        {"id": 3, "severity": "Moderate", "weight": 3},  # drifted from "Medium"
        {"id": 4, "severity": "High", "weight": 4},
        {"id": 5, "severity": "Critical", "weight": 5},
    ]
    with pytest.raises(EnumDriftError, match="findingSeverity"):
        sync(tmp_path, fake)


def test_sync_aborts_on_finding_type_table_drift(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.fetch_finding_types = lambda: [
        {"id": 1, "findingType": "Network"},
        {"id": 2, "findingType": "Physical"},
        {"id": 3, "findingType": "Wireless"},
        {"id": 4, "findingType": "Application"},  # drifted from "Web"
        {"id": 5, "findingType": "Mobile"},
        {"id": 6, "findingType": "Cloud"},
        {"id": 7, "findingType": "Host"},
    ]
    with pytest.raises(EnumDriftError, match="findingType"):
        sync(tmp_path, fake)


def test_sync_aborts_on_unknown_severity_row(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.fetch_finding_severities = lambda: [
        *FakeGW().fetch_finding_severities(),
        {"id": 6, "severity": "Custom", "weight": 6},  # unknown to grison
    ]
    with pytest.raises(EnumDriftError, match="unknown"):
        sync(tmp_path, fake)


# --- warnings wiring: dropped constructs surface on pull, not on every unrelated sync --


def test_pull_surfaces_dropped_styling_span_as_warning_once(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.findings[1] = {
        "id": 1, "title": "Colorful", "severityId": 3, "findingTypeId": 4,
        "cvssVector": "", "cvssScore": None,
        "description": (
            '<p><span data-color="#ff0000" style="color: #ff0000;">critical</span></p>'
        ),
        "impact": "", "mitigation": "", "references": "", "replication_steps": "",
    }

    r = sync(tmp_path, fake)
    libf = tmp_path / "findings" / "library" / "colorful.md"
    assert libf in r.pulled
    assert any("description" in w and "styling span dropped" in w for w in r.warnings)

    r2 = sync(tmp_path, fake)  # nothing changed — must not re-warn every routine sync
    assert r2.warnings == [] and libf in r2.unchanged
