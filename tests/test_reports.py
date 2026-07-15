"""Report-narrative 3-way sync (report.extraFields) via a fake GW client."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from grison.remote import snapshot as snapshot_mod
from grison.remote.reports import sync_reports


class FakeGW:
    """In-memory Ghostwriter report surface. update_report replaces the whole
    extraFields jsonb, exactly like Hasura's ``_set``."""

    def __init__(self) -> None:
        self.reports: dict[int, dict] = {}

    def add_report(self, rid: int, title: str, extra: dict, **meta) -> None:
        self.reports[rid] = {
            "id": rid, "title": title, "extraFields": dict(extra),
            "complete": meta.get("complete", False),
            "archived": meta.get("archived", False),
            "delivered": meta.get("delivered", False),
            "creation": meta.get("creation"), "last_update": meta.get("last_update"),
            "project": meta.get("project"),
        }

    def fetch_reports(self):
        return [dict(r) for r in self.reports.values()]

    def update_report(self, report_id: int, fields: dict) -> None:
        self.reports[report_id]["extraFields"] = dict(fields["extraFields"])


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


def _sec(root: Path, rid: int, slug: str, key: str) -> Path:
    return root / "findings" / "reports" / f"{rid}-{slug}" / "narrative" / f"{key}.md"


def test_pull_creates_section_files_and_meta(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme Pentest", {
        "executive_summary": "<h3>Summary</h3><p>All good.</p>",
        "methodology": "<h2>Plan</h2><p>Steps.</p>",
    }, project={"id": 1, "client": {"id": 2, "name": "Acme", "shortName": "ACM"}})
    r = sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme-pentest", "executive_summary")
    assert es in r.pulled and es.exists()
    assert es.read_text() == "### Summary\n\nAll good.\n"
    meta = yaml.safe_load((es.parent.parent / ".report.yml").read_text())
    assert meta["grison"]["gw"]["report_id"] == 6
    assert meta["project"]["client"]["name"] == "Acme"  # read-only metadata mirror
    assert set(meta["sections"]) == {"executive_summary", "methodology"}
    # idempotent
    r2 = sync_reports(tmp_path, fake)
    assert not r2.pulled and len(r2.unchanged) == 2


def test_local_edit_pushes_only_that_section(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {
        "executive_summary": "<p>old summary</p>",
        "methodology": "<p>untouched</p>",
    })
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("new summary\n")
    r = sync_reports(tmp_path, fake)
    assert es in r.pushed
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>new summary</p>"
    assert fake.reports[6]["extraFields"]["methodology"] == "<p>untouched</p>"  # verbatim kept
    assert sync_reports(tmp_path, fake).unchanged  # converged, no re-push


def test_remote_edit_pulls(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>v1</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    fake.reports[6]["extraFields"]["executive_summary"] = "<p>v2 remote</p>"
    r = sync_reports(tmp_path, fake)
    assert es in r.pulled
    assert es.read_text().strip() == "v2 remote"


def test_both_sides_change_is_collision(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>base</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("LOCAL\n")
    fake.reports[6]["extraFields"]["executive_summary"] = "<p>REMOTE</p>"
    r = sync_reports(tmp_path, fake)
    assert es in r.collisions
    assert es.read_text().strip() == "LOCAL"  # never overwritten
    assert es.with_name("executive_summary.remote.md").read_text().strip() == "REMOTE"
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>REMOTE</p>"  # remote intact


def test_push_is_snapshot_rollbackable(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>original</p>", "methodology": "<p>m</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("edited\n")
    from grison.remote.snapshot import Snapshot, Undo

    r = sync_reports(tmp_path, fake)
    assert r.snapshot_dir is not None
    undo = json_undo(r.snapshot_dir)
    Snapshot(undos=[Undo(**u) for u in undo]).rollback(fake)
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>original</p>"  # restored
    assert fake.reports[6]["extraFields"]["methodology"] == "<p>m</p>"


def test_report_with_no_findings_still_mirrors(tmp_path: Path) -> None:
    """A report with zero findings (no dir yet) still gets its narrative + meta."""
    fake = FakeGW()
    fake.add_report(9, "Empty Report", {"scope_text": "<p>in scope</p>"})
    r = sync_reports(tmp_path, fake)
    st = _sec(tmp_path, 9, "empty-report", "scope_text")
    assert st in r.pulled and st.exists()
    assert (st.parent.parent / ".report.yml").exists()


def test_unknown_local_key_is_skipped_not_pushed(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"})
    sync_reports(tmp_path, fake)
    rogue = _sec(tmp_path, 6, "acme", "made_up_section")
    rogue.write_text("invented\n")
    r = sync_reports(tmp_path, fake)
    assert any(p == rogue and "unknown report field" in note for p, note in r.skipped)
    assert "made_up_section" not in fake.reports[6]["extraFields"]  # never created remotely


def test_force_remote_resolves_collision(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>base</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("LOCAL\n")
    fake.reports[6]["extraFields"]["executive_summary"] = "<p>REMOTE</p>"
    sync_reports(tmp_path, fake)  # collision
    r = sync_reports(tmp_path, fake, force_remote={es})
    assert es in r.pulled
    assert es.read_text().strip() == "REMOTE"


def json_undo(snapshot_dir: Path) -> list[dict]:
    import json
    return json.loads((snapshot_dir / "undo.json").read_text())
