"""Track 3 guard rails: report-narrative crash isolation (F1), the refetch-before-push
collision guard (F2), removed-section marker persistence across syncs (F3), and CLI
phase isolation across the findings/reports/methodology sync phases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from grison.remote import snapshot as snapshot_mod
from grison.remote.methodology import MethResult
from grison.remote.repmap import section_hash
from grison.remote.reports import ReportResult, sync_reports
from grison.remote.sync import SyncResult
from grison.state import StateStore


class FakeGW:
    """In-memory Ghostwriter report surface (mirrors tests/test_reports.py's double)
    plus an ``on_fetch(call_index)`` hook so a test can inject a concurrent remote edit
    between the top-of-run snapshot and a report's own pre-push refetch."""

    def __init__(self) -> None:
        self.reports: dict[int, dict] = {}
        self.fetch_calls = 0
        self.on_fetch = None  # Callable[[int], None] | None

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
        self.fetch_calls += 1
        if self.on_fetch:
            self.on_fetch(self.fetch_calls)
        return [dict(r) for r in self.reports.values()]

    def update_report(self, report_id: int, fields: dict) -> None:
        self.reports[report_id]["extraFields"] = dict(fields["extraFields"])


@pytest.fixture(autouse=True)
def _snap_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "SNAPSHOT_ROOT", tmp_path / "snapshots")


def _rdir(root: Path, rid: int) -> Path:
    hits = sorted((root / "findings" / "reports").glob(f"{rid}-*"))
    assert hits, f"no report dir materialized for report {rid}"
    return hits[0]


def _sec(root: Path, rid: int, key: str) -> Path:
    return _rdir(root, rid) / "narrative" / f"{key}.md"


# ---------------------------------------------------------------------------
# F1 (crash) — unsupported report-narrative HTML must not sink the whole batch
# ---------------------------------------------------------------------------


def test_bad_report_html_is_isolated_other_reports_still_sync(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(5, "Broken", {"scope_text": "<table><tr><td>x</td></tr></table>"})
    fake.add_report(6, "Good", {"executive_summary": "<p>fine</p>"})

    r = sync_reports(tmp_path, fake)  # must not raise

    assert any("5" in e for e in r.errors)
    good = _sec(tmp_path, 6, "executive_summary")
    assert good in r.pulled and good.exists()
    # the broken report never materialized any local state
    assert not list((tmp_path / "findings" / "reports").glob("5-*"))


def test_bad_report_html_still_isolated_on_a_later_sync(tmp_path: Path) -> None:
    """Same as above but the corruption appears on a re-sync of an already-synced
    report, not just first pull — reports.py:113-119's planning loop has no try/except
    the very first time either, but this pins the steady-state case too."""
    fake = FakeGW()
    fake.add_report(5, "Ok For Now", {"scope_text": "<p>fine</p>"})
    fake.add_report(6, "Good", {"executive_summary": "<p>fine</p>"})
    sync_reports(tmp_path, fake)

    fake.reports[5]["extraFields"]["scope_text"] = "<blockquote>nope</blockquote>"
    r = sync_reports(tmp_path, fake)

    assert any("5" in e for e in r.errors)
    good = _sec(tmp_path, 6, "executive_summary")
    assert good in r.unchanged


# ---------------------------------------------------------------------------
# F2 — refetch-before-push collision guard
# ---------------------------------------------------------------------------


def test_concurrent_remote_edit_to_untouched_section_aborts_push_as_collision(
    tmp_path: Path,
) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {
        "executive_summary": "<p>old summary</p>",
        "methodology": "<p>m1</p>",
    })
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "executive_summary")
    meth = _sec(tmp_path, 6, "methodology")
    es.write_text("new summary\n")  # local edit — this section alone would push

    fake.fetch_calls = 0  # call 1 = this run's top-of-run snapshot; call 2 = the guard's refetch

    def on_fetch(call_index: int) -> None:
        if call_index == 2:
            fake.reports[6]["extraFields"]["methodology"] = "<p>m2 concurrent</p>"

    fake.on_fetch = on_fetch
    r = sync_reports(tmp_path, fake)

    # the whole report's push is withheld, not just the drifted section
    assert es not in r.pushed
    assert any(p == es and "push withheld" in note for p, note in r.skipped)
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>old summary</p>"  # untouched
    assert es.read_text().strip() == "new summary"  # the local edit itself survives, just unsent

    # the drifted, untouched section surfaces as an ordinary collision
    assert meth in r.collisions
    sidecar = meth.with_name("methodology.remote.md")
    assert sidecar.read_text().strip() == "m2 concurrent"
    assert meth.read_text().strip() == "m1"  # local narrative file itself untouched

    # nothing was written remotely, so there was nothing to snapshot for undo
    assert r.snapshot_dir is None


def test_push_merges_over_fresh_fetch_not_stale_snapshot(tmp_path: Path) -> None:
    """A key that appears remotely only between the top-of-run snapshot and the actual
    write must survive the push — proves the merge base (and the undo pre-image) is the
    fresh re-fetch immediately before the write, not the stale top-of-run snapshot."""
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>old</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "executive_summary")
    es.write_text("new\n")

    fake.fetch_calls = 0

    def on_fetch(call_index: int) -> None:
        if call_index == 2:
            fake.reports[6]["extraFields"]["new_field"] = "<p>brand new</p>"

    fake.on_fetch = on_fetch
    r = sync_reports(tmp_path, fake)

    assert es in r.pushed
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>new</p>"
    assert fake.reports[6]["extraFields"]["new_field"] == "<p>brand new</p>"  # not wiped

    assert r.snapshot_dir is not None
    undo = json.loads((r.snapshot_dir / "undo.json").read_text())
    pre_image = undo[0]["fields"]["extraFields"]
    assert pre_image["new_field"] == "<p>brand new</p>"  # pre-image is the FRESH fetch


def test_report_gone_remotely_mid_run_withholds_push(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>old</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "executive_summary")
    es.write_text("new\n")

    fake.fetch_calls = 0

    def on_fetch(call_index: int) -> None:
        if call_index == 2:
            del fake.reports[6]

    fake.on_fetch = on_fetch
    r = sync_reports(tmp_path, fake)

    assert es not in r.pushed
    assert any("no longer exists remotely" in e for e in r.errors)


# ---------------------------------------------------------------------------
# F3 (section-gone marker) — must survive more than one sync cycle
# ---------------------------------------------------------------------------


def test_removed_remotely_marker_persists_across_two_syncs(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>v1</p>", "methodology": "<p>m</p>"})
    sync_reports(tmp_path, fake)
    meth = _sec(tmp_path, 6, "methodology")
    state0 = StateStore(tmp_path).get_report(6)
    assert "methodology" not in state0.removed_remotely

    del fake.reports[6]["extraFields"]["methodology"]  # the field disappears remotely

    r1 = sync_reports(tmp_path, fake)
    assert any(p == meth and "remote section gone" in note for p, note in r1.skipped)
    state1 = StateStore(tmp_path).get_report(6)
    assert "methodology" in state1.removed_remotely

    r2 = sync_reports(tmp_path, fake)  # second consecutive sync — nothing else changes
    assert any(p == meth and "remote section gone" in note for p, note in r2.skipped)
    state2 = StateStore(tmp_path).get_report(6)
    assert "methodology" in state2.removed_remotely
    assert state2.sections["methodology"] == state1.sections["methodology"]


def test_removed_remotely_marker_clears_once_local_file_is_deleted(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>v1</p>", "methodology": "<p>m</p>"})
    sync_reports(tmp_path, fake)
    meth = _sec(tmp_path, 6, "methodology")

    del fake.reports[6]["extraFields"]["methodology"]
    sync_reports(tmp_path, fake)
    assert "methodology" in StateStore(tmp_path).get_report(6).sections

    meth.unlink()
    sync_reports(tmp_path, fake)
    assert "methodology" not in StateStore(tmp_path).get_report(6).sections


# ---------------------------------------------------------------------------
# SSOT — section merge bases live in the private state store, never in
# .report.yml, so a git checkout of the mirror can't resurrect a stale base.
# ---------------------------------------------------------------------------


def test_push_base_lands_in_store_not_report_yml_and_reclassifies_clean(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>base</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "executive_summary")
    es.write_text("edited\n")

    r = sync_reports(tmp_path, fake)
    assert es in r.pushed  # push + pull-after-push canonicalization

    meta = yaml.safe_load((_rdir(tmp_path, 6) / ".report.yml").read_text())
    assert "sections" not in meta  # pure read-only mirror — no merge state

    state = StateStore(tmp_path).get_report(6)
    assert state.sections["executive_summary"] == section_hash(es.read_text())

    r2 = sync_reports(tmp_path, fake)  # base in the store reclassifies the section clean
    assert es in r2.unchanged
    assert es not in r2.pushed and es not in r2.pulled


# ---------------------------------------------------------------------------
# CLI phase isolation (findings / reports / methodology)
# ---------------------------------------------------------------------------


def _set_gw_and_bs_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRISON_GW_URL", "http://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GRISON_BS_URL", "http://bs.test")
    monkeypatch.setenv("GRISON_BS_TOKEN_ID", "bsid")
    monkeypatch.setenv("GRISON_BS_TOKEN_SECRET", "bssecret")


def test_cli_isolates_findings_phase_crash_reports_and_methodology_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grison.cli as cli_mod

    monkeypatch.chdir(tmp_path)
    _set_gw_and_bs_creds(monkeypatch)

    calls: list[str] = []

    def boom_run_sync(root, client, *, dry_run=False, force_local=None, force_remote=None,
                       on_event=None):
        calls.append("findings")
        raise RuntimeError("findings blew up")

    def ok_sync_reports(root, client, *, dry_run=False, force_local=None, force_remote=None,
                         on_event=None):
        calls.append("report")
        return ReportResult()

    def ok_sync_methodology(root, bs, *, dry_run=False, force_local=None, force_remote=None,
                             on_event=None):
        calls.append("methodology")
        return MethResult()

    monkeypatch.setattr(cli_mod, "run_sync", boom_run_sync)
    monkeypatch.setattr(cli_mod, "sync_reports", ok_sync_reports)
    monkeypatch.setattr(cli_mod, "sync_methodology", ok_sync_methodology)

    r = CliRunner().invoke(cli_mod.app, ["sync"])

    assert calls == ["findings", "report", "methodology"]  # later phases ran despite the crash
    assert r.exit_code == 1
    assert "findings sync failed" in r.output and "findings blew up" in r.output


def test_cli_isolates_methodology_phase_crash_after_clean_findings_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grison.cli as cli_mod

    monkeypatch.chdir(tmp_path)
    _set_gw_and_bs_creds(monkeypatch)

    def ok_run_sync(root, client, *, dry_run=False, force_local=None, force_remote=None,
                     on_event=None):
        return SyncResult()

    def ok_sync_reports(root, client, *, dry_run=False, force_local=None, force_remote=None,
                         on_event=None):
        return ReportResult()

    def boom_sync_methodology(root, bs, *, dry_run=False, force_local=None, force_remote=None,
                               on_event=None):
        raise RuntimeError("methodology blew up")

    monkeypatch.setattr(cli_mod, "run_sync", ok_run_sync)
    monkeypatch.setattr(cli_mod, "sync_reports", ok_sync_reports)
    monkeypatch.setattr(cli_mod, "sync_methodology", boom_sync_methodology)

    r = CliRunner().invoke(cli_mod.app, ["sync"])

    # a clean findings+reports run must not mask the methodology-phase crash
    assert r.exit_code == 1
    assert "methodology sync failed" in r.output and "methodology blew up" in r.output


def test_cli_exits_nonzero_on_report_scope_failures_without_aborting_other_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing-scope trip-wire is a lint, not a crash: it must still fail the run
    (same mechanism as collisions/errors) while letting findings and methodology run
    to completion normally."""
    import grison.cli as cli_mod

    monkeypatch.chdir(tmp_path)
    _set_gw_and_bs_creds(monkeypatch)

    calls: list[str] = []

    def ok_run_sync(root, client, *, dry_run=False, force_local=None, force_remote=None,
                     on_event=None):
        calls.append("findings")
        return SyncResult()

    def scope_failing_sync_reports(root, client, *, dry_run=False, force_local=None,
                                    force_remote=None, on_event=None):
        calls.append("report")
        return ReportResult(
            scope_failures=["report 5 (NoScope): project has no scope defined"]
        )

    def ok_sync_methodology(root, bs, *, dry_run=False, force_local=None, force_remote=None,
                             on_event=None):
        calls.append("methodology")
        return MethResult()

    monkeypatch.setattr(cli_mod, "run_sync", ok_run_sync)
    monkeypatch.setattr(cli_mod, "sync_reports", scope_failing_sync_reports)
    monkeypatch.setattr(cli_mod, "sync_methodology", ok_sync_methodology)

    r = CliRunner().invoke(cli_mod.app, ["sync"])

    assert calls == ["findings", "report", "methodology"]  # methodology still ran
    assert r.exit_code == 1
    assert "no scope defined" in r.output
