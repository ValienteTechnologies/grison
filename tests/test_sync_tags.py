"""Track 1a: two-way Ghostwriter tag/CWE sync via setTags/taggedItem (fake, offline).

Mirrors the FakeGW/fixture idiom in test_sync_push.py, plus a tag store keyed by
``(table, gw_id)`` — GW's real taggedItem/setTags surface, minus content-type
resolution (grison.remote.ghostwriter.GhostwriterClient handles that; the sync
engine only ever talks to ``fetch_tag_map()``/``set_tags()``).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from grison.markdown import finding_to_markdown, markdown_to_finding
from grison.model import Finding
from grison.remote import snapshot as snapshot_mod
from grison.remote.gwmap import finding_to_gw_fields, finding_to_gw_tags, stamp_synced
from grison.remote.snapshot import Snapshot, Undo
from grison.remote.sync import sync
from grison.sinks.file_sink import slugify


class FakeGW:
    def __init__(self) -> None:
        self.findings: dict[int, dict] = {}
        self.reported: dict[int, dict] = {}
        self.evidence: dict[int, dict] = {}
        self.images: dict[int, bytes] = {}
        self.tags: dict[tuple[str, int], list[str]] = {}
        self.set_tags_calls: list[tuple[int, str, list[str]]] = []
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
        self.set_tags_calls.append((record_id, table, list(tags)))
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

    def upload_evidence(self, *, finding_id, filename, caption, friendly_name, file_base64) -> int:
        i = self._id()
        self.images[i] = base64.b64decode(file_base64)
        self.evidence[i] = {
            "id": i, "findingId": finding_id, "reportId": None,
            "document": f"evidence/{filename}", "caption": caption, "friendlyName": friendly_name,
        }
        return i

    def delete_evidence(self, evidence_id: int) -> None:
        self.evidence.pop(evidence_id, None)
        self.images.pop(evidence_id, None)

    def delete_finding(self, finding_id: int) -> None:
        self.findings.pop(finding_id, None)

    def delete_reported_finding(self, rid: int) -> None:
        self.reported.pop(rid, None)


def _finding(
    *, tier="library", gw_id=None, report_id=None, title="T", desc="body",
    cwe=None, tags=None, **extra,
) -> Finding:
    data: dict = {
        "grison": {"tier": tier, "gw": {"id": gw_id}},
        "severity": "medium", "finding_type": "web", "title": title, "description": desc,
        "cwe": cwe or [], "tags": tags or [],
    }
    if report_id is not None:
        data["grison"]["gw"]["report_id"] = report_id
    data.update(extra)
    return Finding.model_validate(data)


def _write(path: Path, f: Finding) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(finding_to_markdown(f))


def _seed_synced(
    root: Path, fake: FakeGW, *, tier="library", report_id=None, title="T", desc="body",
    cwe=None, tags=None,
):
    """Insert a remote record (+ its tags, if any) and write a matching, in-sync local file."""
    finding = _finding(tier=tier, report_id=report_id, title=title, desc=desc, cwe=cwe, tags=tags)
    fields = finding_to_gw_fields(finding)
    rid = fake.insert_finding(fields) if tier == "library" else fake.insert_reported_finding(fields)
    table = "finding" if tier == "library" else "reportedFinding"
    remote_tags = finding_to_gw_tags(finding)
    if remote_tags:
        fake.tags[(table, rid)] = remote_tags
    f = _finding(tier=tier, gw_id=rid, report_id=report_id, title=title, desc=desc,
                 cwe=cwe, tags=tags)
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


# --- push ------------------------------------------------------------------------------


def test_tag_only_local_edit_pushes_exactly_the_projected_set(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Tagged")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["cwe"] = ["CWE-79"]
    data["tags"] = ["recon", "internal"]
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert path in r.pushed
    assert fake.set_tags_calls == [(rid, "finding", ["CWE:79", "recon", "internal"])]
    assert fake.tags[("finding", rid)] == ["CWE:79", "recon", "internal"]


def test_push_projects_colon_form(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="ColonForm")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["cwe"] = ["CWE-89"]
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert path in r.pushed
    assert fake.tags[("finding", rid)] == ["CWE:89"]


def test_no_set_tags_call_when_unchanged(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Stable", tags=["keep"])
    path.write_text(path.read_text().replace("body", "edited body"))  # unrelated push trigger
    r = sync(tmp_path, fake)
    assert path in r.pushed
    assert fake.set_tags_calls == []  # tags unchanged — no redundant mutation
    assert fake.tags[("finding", rid)] == ["keep"]


def test_insert_new_finding_pushes_initial_tags(tmp_path: Path) -> None:
    fake = FakeGW()
    path = tmp_path / "findings" / "library" / "brand-new.md"
    _write(path, _finding(title="Brand New", desc="fresh", cwe=["CWE-79"], tags=["recon"]))
    r = sync(tmp_path, fake)
    assert path in r.inserted
    new_id = next(iter(fake.findings))
    assert fake.tags[("finding", new_id)] == ["CWE:79", "recon"]


def test_insert_without_tags_skips_set_tags_call(tmp_path: Path) -> None:
    fake = FakeGW()
    path = tmp_path / "findings" / "library" / "plain.md"
    _write(path, _finding(title="Plain", desc="fresh"))
    sync(tmp_path, fake)
    assert fake.set_tags_calls == []


# --- pull --------------------------------------------------------------------------------


def test_remote_tag_edit_pulls_and_splits_cwe(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Pullable")
    fake.tags[("finding", rid)] = ["CWE:79", "recon"]  # changed on GW, nothing else touched
    r = sync(tmp_path, fake)
    assert path in r.pulled
    f = markdown_to_finding(path.read_text())
    assert f.cwe == ["CWE-79"]
    assert f.tags == ["recon"]
    assert sync(tmp_path, fake).unchanged == [path]  # settles clean


def test_junk_tags_round_trip_untouched(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Junky")
    fake.tags[("finding", rid)] = ["CWE:", "CWE:319 ATT&CK:TA0006 ATT&CK:T1040", "CWE:79"]
    r = sync(tmp_path, fake)
    assert path in r.pulled
    f = markdown_to_finding(path.read_text())
    assert f.cwe == ["CWE-79"]
    assert f.tags == ["CWE:", "CWE:319 ATT&CK:TA0006 ATT&CK:T1040"]  # junk never invented/dropped
    assert sync(tmp_path, fake).unchanged == [path]  # settles clean


def test_colon_dash_space_cwe_variants_normalize_on_pull(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Variants")
    fake.tags[("finding", rid)] = ["CWE-79", "cwe: 79", "CWE:  79"]
    r = sync(tmp_path, fake)
    assert path in r.pulled
    f = markdown_to_finding(path.read_text())
    assert f.cwe == ["CWE-79", "CWE-79", "CWE-79"]
    assert f.tags == []


# --- hash-schema migration (shared design ruling) ------------------------------------------


def test_stale_base_from_pre_tag_hash_schema_repairs_not_collides(tmp_path: Path) -> None:
    """A base stamped before cwe/tags joined content_hash (pre-Track-1a) must converge via
    repair, not a phantom collision, once local and remote agree under the new schema —
    the existing local_hash==remote_hash short-circuit in _classify already covers this."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="PreUpgrade")
    f = markdown_to_finding(path.read_text())
    f.grison.synced.hash = "sha256:stale-pre-tags-schema"  # simulates an old-schema base
    _write(path, f)
    r = sync(tmp_path, fake)
    assert path in r.repaired
    assert not r.collisions and not r.pushed


# --- collision / undo ---------------------------------------------------------------------


def test_collision_when_both_sides_changed_tags(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="BothChange")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["tags"] = ["local-tag"]
    _write(path, Finding.model_validate(data))
    fake.tags[("finding", rid)] = ["remote-tag"]  # changed on GW too
    r = sync(tmp_path, fake)
    assert path in r.collisions
    assert markdown_to_finding(path.read_text()).tags == ["local-tag"]  # never clobbered
    sidecar = path.with_suffix(".remote.md")
    assert sidecar.exists()
    assert markdown_to_finding(sidecar.read_text()).tags == ["remote-tag"]
    assert fake.tags[("finding", rid)] == ["remote-tag"]  # remote untouched
    assert fake.set_tags_calls == []


def test_undo_restores_pre_image_tags(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Undo", tags=["old-tag"])
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["tags"] = ["new-tag"]
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert fake.tags[("finding", rid)] == ["new-tag"]
    assert r.snapshot_dir is not None
    raw = json.loads((r.snapshot_dir / "undo.json").read_text(encoding="utf-8"))
    snap = Snapshot(undos=[Undo(**u) for u in raw])
    snap.rollback(fake)
    assert fake.tags[("finding", rid)] == ["old-tag"]
