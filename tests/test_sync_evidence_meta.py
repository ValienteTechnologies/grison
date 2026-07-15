"""Track 1b tests: evidence caption/friendly_name/description two-way sync (per-image
3-way via ``EvidenceGwRef.meta``) + the guards that ride along with it (missing-on-disk
re-download, local rename guard, tier-relocate refusal). No live calls — a fake
in-memory read+write Ghostwriter, mirroring tests/test_sync_push.py's FakeGW plus
``update_evidence``/``description`` support."""

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
from grison.remote.sync import pull, sync
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


def _seed_with_evidence(root: Path, fake: FakeGW, *, title="Ev"):
    """A synced instance finding with one already-uploaded evidence image (caption
    'old', friendly_name 'shot', description 'old desc') — the common starting point
    for the meta-drift tests below."""
    path, rid = _seed_synced(root, fake, tier="instance", report_id=2, title=title)
    (path.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (path.parent / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{
        "file": "evidence/shot.png", "caption": "old", "friendly_name": "shot",
        "description": "old desc",
    }]
    _write(path, Finding.model_validate(data))
    sync(root, fake)  # uploads, settles clean
    ev_id = next(iter(fake.evidence))
    return path, rid, ev_id


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


# --- caption/friendly_name/description two-way (gw-push-2 / [evidence] F1) -----------------


def test_caption_only_local_edit_pushes_update_evidence(tmp_path: Path) -> None:
    """Caption drift is invisible to content_hash by design, but must not be stranded
    locally forever: a local-only caption edit pushes via update_evidence — not a
    re-upload — and is reflected in a normal 'push' result."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Cap Push")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "new caption"
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)
    assert path in r.pushed and r.errors == []
    assert fake.evidence[ev_id]["caption"] == "new caption"
    assert len(fake.evidence) == 1  # update-in-place, not a re-upload
    assert r.evidence_up == 0  # not counted as an upload

    r2 = sync(tmp_path, fake)  # settles clean — no push/pull thrash
    assert r2.unchanged == [path] and r2.pushed == []


def test_remote_only_caption_edit_adopted_on_next_sync(tmp_path: Path) -> None:
    """A caption edited only on the Ghostwriter side (e.g. via the web UI) must be
    adopted into the local file, not silently discarded on the next sync."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Cap Pull")
    fake.evidence[ev_id]["caption"] = "changed via GW web UI"

    r = sync(tmp_path, fake)
    assert r.errors == []
    # the whole-record content_hash is unaffected by evidence meta, so this settles
    # through the same 'push' plan branch that reconciles pending/stale evidence —
    # nothing is actually re-sent to Ghostwriter, the local file just adopts remote's
    # value (see _reconcile_evidence_meta's 'remote_ahead' branch).
    assert path in r.pushed
    f2 = markdown_to_finding(path.read_text())
    assert f2.evidence[0].caption == "changed via GW web UI"

    r2 = sync(tmp_path, fake)
    assert r2.unchanged == [path]


def test_both_changed_evidence_meta_collides_and_preserves_local(tmp_path: Path) -> None:
    """Caption changed on both sides between syncs: no silent winner — surfaced as a
    collision (like a body collision), local file untouched, remote untouched."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Cap Collide")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "local caption"
    _write(path, Finding.model_validate(data))
    fake.evidence[ev_id]["caption"] = "remote caption"

    r = sync(tmp_path, fake)
    assert any("evidence metadata collision" in e for e in r.errors)
    f2 = markdown_to_finding(path.read_text())
    assert f2.evidence[0].caption == "local caption"  # never silently overwritten
    assert fake.evidence[ev_id]["caption"] == "remote caption"  # remote untouched


def test_both_changed_evidence_meta_collides_on_pull_path(tmp_path: Path) -> None:
    """Same collision, but the WHOLE record is also classified 'pull' this run (an
    unrelated remote field changed) — exercising _apply_pull's per-image reconcile,
    a separate code path from the push-side one above."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Cap Collide Pull")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "local caption"
    _write(path, Finding.model_validate(data))
    fake.evidence[ev_id]["caption"] = "remote caption"
    fake.reported[rid]["description"] = "<p>remote body change</p>"  # forces a record-level pull

    r = sync(tmp_path, fake)
    assert any("evidence metadata collision" in e for e in r.errors)
    f2 = markdown_to_finding(path.read_text())
    assert "remote body change" in f2.description  # the unrelated remote edit still arrives
    assert f2.evidence[0].caption == "local caption"  # evidence collision still preserves local


def test_force_remote_resolves_evidence_collision(tmp_path: Path) -> None:
    """--force-remote picks remote unconditionally per image too, same as the
    record-level force semantics — no collision surfaced."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Force Remote")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "local caption"
    _write(path, Finding.model_validate(data))
    fake.evidence[ev_id]["caption"] = "remote caption"

    r = sync(tmp_path, fake, force_remote={path})
    assert not any("collision" in e for e in r.errors)
    f2 = markdown_to_finding(path.read_text())
    assert f2.evidence[0].caption == "remote caption"


def test_evidence_description_round_trips_upload_then_pull(tmp_path: Path) -> None:
    """evidence.description ([evidence] F4) is sent on upload and comes back intact
    through a fresh pull elsewhere."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Desc RT")
    (path.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (path.parent / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{
        "file": "evidence/shot.png", "caption": "cap", "friendly_name": "shot",
        "description": "Full compromise chain screenshot",
    }]
    _write(path, Finding.model_validate(data))
    sync(tmp_path, fake)
    ev_id = next(iter(fake.evidence))
    assert fake.evidence[ev_id]["description"] == "Full compromise chain screenshot"

    other = tmp_path / "other-clone"
    r = pull(other, fake)
    assert r.errors == []
    files = list((other / "findings" / "reports").rglob("*.md"))
    assert len(files) == 1
    pulled_f = markdown_to_finding(files[0].read_text())
    assert pulled_f.evidence[0].description == "Full compromise chain screenshot"


def test_undo_restores_old_caption(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Undo Cap")
    before_caption = fake.evidence[ev_id]["caption"]
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["caption"] = "new caption"
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)
    assert fake.evidence[ev_id]["caption"] == "new caption"
    assert r.snapshot_dir is not None
    raw = json.loads((r.snapshot_dir / "undo.json").read_text(encoding="utf-8"))
    assert any(u["op"] == "update_evidence" for u in raw)

    snap = Snapshot(undos=[Undo(**u) for u in raw])
    snap.rollback(fake)
    assert fake.evidence[ev_id]["caption"] == before_caption


# --- missing-on-disk guard ([evidence] F2 / gw-pull scenario) -------------------------------


def test_missing_local_file_with_gw_id_redownloads(tmp_path: Path) -> None:
    """Deleting an already-uploaded image's local file must not stay silently 'clean'
    forever (F2) — the next sync re-downloads it, rather than erroring or ignoring it."""
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Missing File")
    img = path.parent / "evidence" / "shot.png"
    img.unlink()
    assert not img.exists()

    r = sync(tmp_path, fake)
    assert img.exists() and img.read_bytes() == fake.images[ev_id]
    assert r.evidence_down >= 1
    assert not any("missing" in e.lower() for e in r.errors)

    r2 = sync(tmp_path, fake)  # settles clean — no redownload loop
    assert r2.unchanged == [path]


def test_missing_local_file_without_gw_id_errors(tmp_path: Path) -> None:
    """Contrast with the above: a NEW entry (never uploaded, no gw.id) whose file is
    simply absent has nothing to recover from — it must surface loudly, not vanish."""
    fake = FakeGW()
    path, rid = _seed_synced(tmp_path, fake, tier="instance", report_id=2, title="Pending Missing")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"] = [{"file": "evidence/never-added.png", "caption": "x", "friendly_name": "x"}]
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)
    assert any("evidence image missing" in e for e in r.errors)
    assert not fake.evidence


# --- local rename guard ([evidence] F6) ------------------------------------------------------


def test_local_rename_guard_errors(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Renamed")
    (path.parent / "evidence" / "shot.png").rename(path.parent / "evidence" / "prod-bypass.png")
    data = markdown_to_finding(path.read_text()).model_dump(mode="json")
    data["evidence"][0]["file"] = "evidence/prod-bypass.png"
    _write(path, Finding.model_validate(data))

    r = sync(tmp_path, fake)
    assert any("renamed locally" in note for _p, note in r.skipped)
    assert not r.pushed and not r.pulled
    assert fake.evidence[ev_id]["document"] == "evidence/shot.png"  # remote untouched


# --- tier-relocate refusal ([evidence] F3) ---------------------------------------------------


def test_library_move_with_evidence_errors(tmp_path: Path) -> None:
    fake = FakeGW()
    path, rid, ev_id = _seed_with_evidence(tmp_path, fake, title="Lib Move")
    moved = tmp_path / "findings" / "library" / "moved.md"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_text(path.read_text())  # frontmatter still says tier=instance/reportedFinding
    (moved.parent / "evidence").mkdir(parents=True, exist_ok=True)
    (moved.parent / "evidence" / "shot.png").write_bytes(
        (path.parent / "evidence" / "shot.png").read_bytes()
    )

    r = sync(tmp_path, fake)
    assert any("library findings can't carry evidence" in note for _p, note in r.skipped)
    assert not fake.findings  # no new library record created
    assert rid in fake.reported and ev_id in fake.evidence  # original untouched
    assert path.exists()  # the original instance file is unaffected
