"""Phase-8 tests: full 3-way sync (push/pull/collision/move/guards) via a fake
in-memory read+write Ghostwriter. No live calls."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from grison.markdown import finding_to_markdown, markdown_to_finding
from grison.model import Finding
from grison.remote import snapshot as snapshot_mod
from grison.remote.gwmap import finding_to_gw_fields, stamp_synced
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
            {"id": 1, "finding_type": "Network"},
            {"id": 2, "finding_type": "Physical"},
            {"id": 3, "finding_type": "Wireless"},
            {"id": 4, "finding_type": "Web"},
            {"id": 5, "finding_type": "Mobile"},
            {"id": 6, "finding_type": "Cloud"},
            {"id": 7, "finding_type": "Host"},
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
    root: Path, fake: FakeGW, *, tier="library", report_id=None, title="T", desc="body"
):
    """Insert a remote record + write a matching, in-sync local file."""
    fields = finding_to_gw_fields(_finding(tier=tier, report_id=report_id, title=title, desc=desc))
    rid = fake.insert_finding(fields) if tier == "library" else fake.insert_reported_finding(fields)
    f = _finding(tier=tier, gw_id=rid, report_id=report_id, title=title, desc=desc)
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


def test_insert_new_library_finding(tmp_path: Path) -> None:
    fake = FakeGW()
    path = tmp_path / "findings" / "library" / "new-one.md"
    _write(path, _finding(title="New One", desc="fresh"))
    r = sync(tmp_path, fake)
    assert path in r.inserted and len(fake.findings) == 1
    back = markdown_to_finding(path.read_text())
    assert back.grison.gw.id is not None and back.grison.synced.hash  # id + base written back
    r2 = sync(tmp_path, fake)  # now clean
    assert path in r2.unchanged and not r2.pushed and not r2.inserted


def test_push_local_edit(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Edit Me")
    path.write_text(path.read_text().replace("body", "edited body"))
    r = sync(tmp_path, fake)
    assert path in r.pushed
    assert "edited body" in fake.findings[rid]["description"]
    assert sync(tmp_path, fake).unchanged == [path]  # settles clean


def test_pull_remote_only(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.insert_finding(finding_to_gw_fields(_finding(title="Remote Only", desc="r")))
    r = sync(tmp_path, fake)
    pulled = tmp_path / "findings" / "library" / "remote-only.md"
    assert pulled in r.pulled and pulled.exists()


def test_collision_writes_sidecar_and_preserves_local(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Both Change")
    path.write_text(path.read_text().replace("body", "LOCAL change"))
    fake.findings[rid]["description"] = "<p>REMOTE change</p>"
    r = sync(tmp_path, fake)
    assert path in r.collisions
    assert "LOCAL change" in path.read_text()  # never clobbered
    sidecar = path.with_suffix(".remote.md")
    assert sidecar.exists() and "REMOTE change" in sidecar.read_text()
    assert fake.findings[rid]["description"] == "<p>REMOTE change</p>"  # remote untouched


def test_force_local_resolves_collision(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Forced")
    path.write_text(path.read_text().replace("body", "WINNER"))
    fake.findings[rid]["description"] = "<p>loser</p>"
    r = sync(tmp_path, fake, force_local={path})
    assert path in r.pushed and "WINNER" in fake.findings[rid]["description"]


def test_stale_base_self_repair(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Crashy")
    # simulate a crash after the remote write but before the hash write-back: base is stale,
    # but local and remote content are identical.
    f = markdown_to_finding(path.read_text())
    f.grison.synced.hash = "sha256:stale"
    _write(path, f)
    r = sync(tmp_path, fake)
    assert path in r.repaired and not r.pushed and not r.collisions


def test_invalid_broken_link(tmp_path: Path) -> None:
    fake = FakeGW()
    # id set but no synced block → broken link
    f = _finding(gw_id=555, title="Broken")
    _write(tmp_path / "findings" / "library" / "broken.md", f)
    r = sync(tmp_path, fake)
    assert len(r.invalid) == 1 and not r.pushed and not fake.findings


def test_move_library_to_report_inserts_new_record(tmp_path: Path) -> None:
    fake = FakeGW()
    lib_path, lib_id = _seed_synced(tmp_path, fake, title="Graduate Me")
    # cp the library finding into a report dir (frontmatter still says library)
    moved = tmp_path / "findings" / "reports" / "2-acme" / "copied.md"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_text(lib_path.read_text())
    r = sync(tmp_path, fake)
    assert moved in r.inserted
    assert len(fake.reported) == 1  # a NEW reportedFinding in report 2
    new_rf = next(iter(fake.reported.values()))
    assert new_rf["reportId"] == 2
    assert fake.findings[lib_id]  # source library record untouched
    # the moved file is now an instance mirroring the new record
    assert markdown_to_finding(moved.read_text()).grison.tier == "instance"


def test_duplicate_identity_trips_and_skips(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Original")
    dup = path.parent / "backup-copy.md"
    dup.write_text(path.read_text())  # same (table, id)
    r = sync(tmp_path, fake)
    assert any("duplicate identity" in note for _p, note in r.skipped)
    assert not r.pushed and not r.inserted  # nothing written for the duped id


def test_mass_change_guard_blocks_bulk_inserts(tmp_path: Path) -> None:
    fake = FakeGW()  # empty remote → any inserts are a large share
    for i in range(8):
        _write(tmp_path / "findings" / "library" / f"f{i}.md", _finding(title=f"F{i}"))
    r = sync(tmp_path, fake)
    assert r.mass_change_blocked and not fake.findings  # no remote writes happened
    assert not r.inserted


def test_evidence_push_uploads_new_image(tmp_path: Path) -> None:
    fake = FakeGW()
    # an in-sync instance finding with NO evidence yet
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Ev")
    rdir = path.parent
    (rdir / "evidence").mkdir(parents=True, exist_ok=True)
    (rdir / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    # add a local evidence entry (keeps the old base → local is now ahead)
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot"}]
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)
    assert r.evidence_up == 1 and len(fake.evidence) == 1
    ev = next(iter(fake.evidence.values()))
    assert ev["findingId"] == rid and ev["caption"] == "cap"
    # settles clean on re-sync (evidence now has an id, remote has the image)
    assert sync(tmp_path, fake).unchanged == [path]


def test_inbox_is_local_only(tmp_path: Path) -> None:
    """findings/inbox/ is never pushed — parse output stays local until cp'd into a report."""
    fake = FakeGW()
    inbox = tmp_path / "findings" / "inbox" / "proto.md"
    _write(inbox, _finding(tier="instance", title="Proto"))  # gw.id null, in inbox
    r = sync(tmp_path, fake)
    assert inbox not in (r.inserted + r.pushed)
    assert not fake.findings and not fake.reported  # nothing created remotely
    assert inbox.exists()


def test_snapshot_persisted_when_a_record_fails_midbatch(tmp_path: Path) -> None:
    fake = FakeGW()
    good, gid = _seed_synced(tmp_path, fake, title="Good")
    bad, _ = _seed_synced(tmp_path, fake, title="Bad")
    good.write_text(good.read_text().replace("body", "good edit"))
    # a markdown table in the body is outside the GW whitelist → finding_to_gw_fields raises
    bad.write_text(bad.read_text().replace("body", "| a | b |\n| - | - |\n| 1 | 2 |"))

    r = sync(tmp_path, fake)
    assert "good edit" in fake.findings[gid]["description"]  # the good push applied
    assert any(str(bad) in e for e in r.errors)  # the bad one is isolated, not fatal
    # the undo journal for the applied write is still persisted despite the failure
    assert r.snapshot_dir is not None and (r.snapshot_dir / "undo.json").exists()


def test_caption_edit_pushes_update_evidence_once_then_settles_clean(tmp_path: Path) -> None:
    """A caption-only edit on an already-uploaded image is invisible to content_hash
    (by design — see gwmap._syncable_view), but must not be stranded locally forever
    (gw-push-2): it pushes via update_evidence (not a re-upload) exactly once, then the
    record settles clean — no push/pull thrash on repeated syncs."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Cap")
    # attach + upload an image so it has a gw id
    (path.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (path.parent / "evidence" / "s.png").write_bytes(b"\x89PNG\r\n\x1a\nX")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/s.png", "caption": "old", "friendly_name": "s"}]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)  # uploads, settles clean
    assert sync(tmp_path, fake).unchanged == [path]
    ev_id = next(iter(fake.evidence))

    # editing only the caption of an already-uploaded image must reach Ghostwriter...
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "new caption"
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert r.pulled == [] and r.collisions == []
    assert path in r.pushed
    assert fake.evidence[ev_id]["caption"] == "new caption"  # actually reached GW...
    assert len(fake.evidence) == 1  # ...via update-in-place, not a re-upload

    # ...and then must NOT create a push/pull loop
    r2 = sync(tmp_path, fake)
    assert r2.unchanged == [path] and r2.pushed == [] and r2.pulled == []


def test_snapshot_rollback_restores(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Rollback", desc="original")
    before = dict(fake.findings[rid])
    path.write_text(path.read_text().replace("original", "mutated"))
    # capture the snapshot the sync builds by rolling back manually
    snap = Snapshot()
    from grison.remote.gwmap import gw_pre_image
    snap.before_update("finding", rid, gw_pre_image(fake.findings[rid], tier="library"))
    sync(tmp_path, fake)  # pushes "mutated"
    assert "mutated" in fake.findings[rid]["description"]
    snap.rollback(fake)  # undo
    assert fake.findings[rid]["description"] == before["description"]


# --- code-review fixes: identity-drop no-clobber, per-record isolation, evidence -----------


def test_duplicate_identity_skip_does_not_pull_clobber(tmp_path: Path) -> None:
    """A dropped ``key`` on the duplicate-skip plan used to leave the identity unmatched,
    so the remote-only pull loop would treat it as vanished and pull a fresh copy over it."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Original")
    dup = path.parent / "backup-copy.md"
    dup.write_text(path.read_text())  # same (table, id)
    before = path.read_text()
    r = sync(tmp_path, fake)
    assert path not in r.pulled and dup not in r.pulled
    assert path.read_text() == before  # neither copy was clobbered by a pull-back


def test_invalid_broken_link_not_pulled_as_duplicate(tmp_path: Path) -> None:
    """A dropped ``key`` on the invalid plan used to leave the id unmatched, so the
    remote-only loop would pull the still-live remote record down as a second file."""
    fake = FakeGW()
    fake.findings[555] = {"id": 555, **finding_to_gw_fields(_finding(title="Broken Remote"))}
    broken = tmp_path / "findings" / "library" / "broken.md"
    _write(broken, _finding(gw_id=555, title="Broken"))
    r = sync(tmp_path, fake)
    assert broken in r.invalid
    assert r.pulled == []
    assert list((tmp_path / "findings" / "library").glob("*.md")) == [broken]


def test_remote_report_move_surfaces_as_skip(tmp_path: Path) -> None:
    """A record whose id still lives at this location locally, but whose remote record
    moved to a different report, used to insert a duplicate at the OLD report instead of
    surfacing the conflict."""
    fake = FakeGW()
    fake.reports[3] = {"id": 3, "title": "OtherCo"}
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Moved")
    before = path.read_text()
    fake.reported[rid]["reportId"] = 3  # moved to another report on Ghostwriter
    r = sync(tmp_path, fake)
    assert any("moved to report 3" in note for _p, note in r.skipped)
    assert path.read_text() == before  # nothing written locally
    assert fake.reported[rid]["reportId"] == 3  # nothing written remotely
    assert not r.pushed and not r.inserted and not r.pulled


def test_malformed_remote_record_isolated_in_classify(tmp_path: Path) -> None:
    """One local record whose remote counterpart is malformed must not abort the batch,
    and must not be re-clobbered by the remote-only pull fallback."""
    fake = FakeGW()
    good, _good_id = _seed_synced(tmp_path, fake, title="Good One")
    bad, bad_id = _seed_synced(tmp_path, fake, title="Bad One")
    fake.findings[bad_id]["severityId"] = 999  # unknown GW severityId → gw_record_to_finding raises
    r = sync(tmp_path, fake)
    assert any(str(bad) in e for e in r.errors)
    assert good in r.unchanged
    assert bad not in r.pulled
    assert "Bad One" in bad.read_text()  # left untouched, not clobbered


def test_malformed_remote_only_record_isolated(tmp_path: Path) -> None:
    """One malformed remote-only record must not block other remote-only records from
    pulling down."""
    fake = FakeGW()
    fake.insert_finding(finding_to_gw_fields(_finding(title="Remote Good")))
    bad_id = fake.insert_finding(finding_to_gw_fields(_finding(title="Remote Bad")))
    fake.findings[bad_id]["severityId"] = 999
    r = sync(tmp_path, fake)
    good_path = tmp_path / "findings" / "library" / "remote-good.md"
    assert good_path in r.pulled and good_path.exists()
    assert any(f"finding {bad_id}" in e for e in r.errors)
    assert not (tmp_path / "findings" / "library" / "remote-bad.md").exists()


def test_failed_push_not_double_counted_in_pushed(tmp_path: Path) -> None:
    """A record whose remote write never happens (raises before the update call) must not
    appear in ``pushed`` — only in ``errors``."""
    fake = FakeGW()
    bad, _bad_id = _seed_synced(tmp_path, fake, title="Bad Push")
    # a markdown table in the body is outside the GW whitelist → finding_to_gw_fields raises
    bad.write_text(bad.read_text().replace("body", "| a | b |\n| - | - |\n| 1 | 2 |"))
    r = sync(tmp_path, fake)
    assert bad not in r.pushed
    assert any(str(bad) in e for e in r.errors)


def test_evidence_removed_locally_deletes_remote_row_with_undo(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Ev Del")
    rdir = path.parent
    (rdir / "evidence").mkdir(parents=True, exist_ok=True)
    (rdir / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot"}]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)  # uploads, settles clean
    assert len(fake.evidence) == 1
    ev_id = next(iter(fake.evidence))

    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = []  # removed locally
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert r.evidence_deleted == 1
    assert ev_id not in fake.evidence  # gone remotely too

    assert r.snapshot_dir is not None
    undo = json.loads((r.snapshot_dir / "undo.json").read_text(encoding="utf-8"))
    reup = next(u for u in undo if u["op"] == "upload_evidence")
    assert base64.b64decode(reup["fields"]["file_base64"]) == b"\x89PNG\r\n\x1a\nDATA"
    assert reup["fields"]["finding_id"] == rid


def test_evidence_delete_rollback_restores(tmp_path: Path) -> None:
    """The upload_evidence undo op actually restores a deleted row via the real client."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Ev Undo")
    (path.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (path.parent / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot"}]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)
    ev_id = next(iter(fake.evidence))

    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = []
    _write(path, Finding.model_validate(data))
    r = sync(tmp_path, fake)
    assert ev_id not in fake.evidence

    raw = json.loads((r.snapshot_dir / "undo.json").read_text(encoding="utf-8"))
    snap = Snapshot(undos=[Undo(**u) for u in raw])
    snap.rollback(fake)
    assert len(fake.evidence) == 1  # restored (as a new row — GW has no undelete)


def test_changed_evidence_bytes_reuploads_and_deletes_old_row(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Ev Swap")
    rdir = path.parent
    (rdir / "evidence").mkdir(parents=True, exist_ok=True)
    (rdir / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nORIGINAL")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot"}]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)
    old_id = next(iter(fake.evidence))

    # swap the bytes under the SAME filename — the file-set-only merge base can't see this
    (rdir / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nCHANGED")
    r = sync(tmp_path, fake)
    assert r.evidence_up == 1 and r.evidence_deleted == 1
    assert old_id not in fake.evidence
    assert len(fake.evidence) == 1
    new_id = next(iter(fake.evidence))
    assert fake.images[new_id] == b"\x89PNG\r\n\x1a\nCHANGED"

    f2 = markdown_to_finding(path.read_text())
    assert f2.evidence[0].gw is not None and f2.evidence[0].gw.id == new_id


def test_instance_to_instance_move_reuploads_evidence(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.reports[5] = {"id": 5, "title": "Other"}
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Move Me")
    (path.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (path.parent / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot"}]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)  # uploads the image, entry.gw now carries a real (old) row id

    moved = tmp_path / "findings" / "reports" / "5-other" / "copied.md"
    (moved.parent / "evidence").mkdir(parents=True, exist_ok=True)
    moved.write_text(path.read_text())
    (moved.parent / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    r = sync(tmp_path, fake)
    assert moved in r.inserted
    new_rf = next(rec for rid2, rec in fake.reported.items() if rid2 != rid)
    assert new_rf["reportId"] == 5
    assert len(fake.evidence) == 2  # old row left alone, a fresh one attached to the new record
    new_row = next(e for e in fake.evidence.values() if e["findingId"] == new_rf["id"])
    moved_f = markdown_to_finding(moved.read_text())
    assert moved_f.evidence[0].gw is not None and moved_f.evidence[0].gw.id == new_row["id"]


# --- on_event progress callback ------------------------------------------------------------


def test_push_emits_event(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Push Event")
    path.write_text(path.read_text().replace("body", "edited body"))
    events: list[str] = []
    r = sync(tmp_path, fake, on_event=events.append)
    assert path in r.pushed
    assert f"push {path.relative_to(tmp_path)}" in events


def test_pull_emits_event(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.insert_finding(finding_to_gw_fields(_finding(title="Pull Event", desc="r")))
    events: list[str] = []
    r = sync(tmp_path, fake, on_event=events.append)
    pulled = tmp_path / "findings" / "library" / "pull-event.md"
    assert pulled in r.pulled
    assert f"pull {pulled.relative_to(tmp_path)}" in events


def test_collision_emits_event(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Collision Event")
    path.write_text(path.read_text().replace("body", "LOCAL change"))
    fake.findings[rid]["description"] = "<p>REMOTE change</p>"
    events: list[str] = []
    r = sync(tmp_path, fake, on_event=events.append)
    assert path in r.collisions
    assert f"collision {path.relative_to(tmp_path)} → sidecar written" in events


def test_dry_run_emits_would_prefixed_events(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, title="Dry Push")
    path.write_text(path.read_text().replace("body", "edited body"))
    events: list[str] = []
    r = sync(tmp_path, fake, dry_run=True, on_event=events.append)
    assert path in r.pushed
    assert any(e.startswith("would push ") for e in events)
    assert "edited" not in fake.findings[rid]["description"]  # dry-run wrote nothing remotely
