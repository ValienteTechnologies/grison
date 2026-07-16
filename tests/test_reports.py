"""Report-narrative 3-way sync (report.extraFields) via a fake GW client."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from grison.remote import snapshot as snapshot_mod
from grison.remote.reports import sync_reports
from grison.state import StateStore


class FakeGW:
    """In-memory Ghostwriter report surface. update_report replaces the whole
    extraFields jsonb, exactly like Hasura's ``_set``."""

    def __init__(self) -> None:
        self.reports: dict[int, dict] = {}
        # project-note push surface (Track: pull GW project context + append-only notes)
        self.whoami_username = "operator1"
        self.user_ids = {"operator1": 42}
        self.next_note_id = 100
        self.notes_inserted: list[dict] = []

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

    def whoami(self) -> dict:
        return {"username": self.whoami_username, "role": "user", "expires": None}

    def resolve_user_id(self, username: str) -> int | None:
        return self.user_ids.get(username)

    def insert_project_note(
        self, project_id: int, note_html: str, operator_id: int, timestamp
    ) -> int:
        note_id = self.next_note_id
        self.next_note_id += 1
        self.notes_inserted.append(
            {
                "projectId": project_id,
                "note": note_html,
                "operatorId": operator_id,
                "timestamp": timestamp,
            }
        )
        return note_id


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
    assert "sections" not in meta  # merge bases live in the state store, not the mirror
    state = StateStore(tmp_path).get_report(6)
    assert set(state.sections) == {"executive_summary", "methodology"}
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


# --- warnings wiring: dropped constructs surface on pull, not on every unrelated sync -----


def test_pull_surfaces_dropped_styling_span_as_warning_once(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {
        "executive_summary": (
            '<p><span data-color="#ff0000" style="color: #ff0000;">urgent</span></p>'
        ),
    })
    r = sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    assert es in r.pulled
    assert any("executive_summary" in w and "styling span dropped" in w for w in r.warnings)

    r2 = sync_reports(tmp_path, fake)  # nothing changed — must not re-warn every routine sync
    assert r2.warnings == [] and es in r2.unchanged


# --- push stamps from the remote-rebuilt form, not the raw local markdown -----------


def test_push_of_noncanonical_markdown_stamps_base_from_rebuilt_form(tmp_path: Path) -> None:
    """``* `` bullets are valid push input (md_to_html accepts them) but html_to_md
    always re-emits ``- `` — so a push of unmodified ``* `` markdown is NOT a fixed
    point. Without the fix, the base is stamped from the raw local text and the very
    next sync sees phantom remote drift (a pull) for a section nobody touched."""
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>base</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("* item one\n* item two\n")

    r = sync_reports(tmp_path, fake)
    assert es in r.pushed

    # local file rewritten to the canonical form the next pull would produce
    assert es.read_text().strip() == "- item one\n- item two"
    assert fake.reports[6]["extraFields"]["executive_summary"] == (
        "<ul><li>item one</li><li>item two</li></ul>"
    )
    from grison.remote.repmap import section_hash

    state = StateStore(tmp_path).get_report(6)
    assert state.sections["executive_summary"] == section_hash(es.read_text())
    meta = yaml.safe_load((es.parent.parent / ".report.yml").read_text())
    assert "sections" not in meta  # base lives in the store, not the mirror

    r2 = sync_reports(tmp_path, fake)  # nothing actually changed since the push
    assert es in r2.unchanged
    assert es not in r2.pulled and es not in r2.pushed


def test_push_canonicalization_failure_surfaces_error_and_leaves_base_unstamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push itself (``update_report``) succeeded — GW has the new HTML — but the
    post-push html->md rebuild used to compute the canonical base blew up. The section
    must not be silently stamped from the local markdown (that's the exact bug this
    module fixes); it's left base-less so the next sync reclassifies it explicitly."""
    import grison.remote.reports as reports_mod

    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>base</p>", "methodology": "<p>m</p>"})
    sync_reports(tmp_path, fake)
    es = _sec(tmp_path, 6, "acme", "executive_summary")
    es.write_text("edited\n")

    def boom(html, *, on_loss=None):
        raise ValueError("boom")

    with monkeypatch.context() as m:
        m.setattr(reports_mod, "html_section_to_md", boom)
        r = sync_reports(tmp_path, fake)

    assert es in r.pushed  # the write to GW happened regardless
    assert fake.reports[6]["extraFields"]["executive_summary"] == "<p>edited</p>"
    assert any(
        "could not re-canonicalize section" in e
        and "executive_summary" in e
        and "re-run sync to reclassify" in e
        for e in r.errors
    )
    assert es.read_text().strip() == "edited"  # rebuild failed — nothing to write back

    state = StateStore(tmp_path).get_report(6)
    assert "executive_summary" not in state.sections  # base left unstamped
    assert "methodology" in state.sections  # untouched section unaffected
    meta = yaml.safe_load((es.parent.parent / ".report.yml").read_text())
    assert "sections" not in meta  # mirror never carried merge state

    r2 = sync_reports(tmp_path, fake)  # re-run without the injected failure: reclassifies
    assert es in r2.repaired
    r3 = sync_reports(tmp_path, fake)
    assert es in r3.unchanged


# --- project context mirror (project.md) + append-only project notes ----------------


_TURKISH_PROJECT = {
    "id": 1,
    "codename": "Şahin Operasyonu",
    "client": {"id": 2, "name": "Acme Türkiye", "shortName": "ACM"},
    "startDate": "2026-01-01",
    "endDate": "2026-02-01",
    "collab_note": "<p>Türkçe not: dikkatli ol.</p>",
    "scopes": [
        {
            "name": "Internal",
            "scope": "10.0.0.0/8\r\n10.1.1.1",
            "description": "İç ağ",
            "disallowed": False,
            "requiresCaution": True,
        },
        {
            "name": "Excluded hosts",
            "scope": "10.9.9.9",
            "description": "",
            "disallowed": True,
            "requiresCaution": False,
        },
    ],
    "objectives": [],
    "targets": [],
    "whitecards": [
        {
            "title": "Kill switch",
            "issued": "2026-01-10T00:00:00Z",
            "description": "<p>Acil durdurma <strong>talimatı</strong></p>",
        }
    ],
    "comments": [],
}


def _project_md(root: Path, rid: int, slug: str) -> Path:
    return root / "findings" / "reports" / f"{rid}-{slug}" / "project.md"


def _notes_dir(root: Path, rid: int, slug: str) -> Path:
    return root / "findings" / "reports" / f"{rid}-{slug}" / "notes"


def test_project_context_md_renders_scope_flags_and_turkish_content(tmp_path: Path) -> None:
    """Golden-ish render test: Turkish strings, a disallowed scope, a caution scope, a
    whitecard with HTML description — content + EXCLUDED/CAUTION markers must survive."""
    from grison.remote.repmap import project_context_to_md

    md = project_context_to_md(_TURKISH_PROJECT)
    assert "Şahin Operasyonu" in md
    assert "Acme Türkiye" in md
    assert "İç ağ" in md
    assert "Türkçe not: dikkatli ol." in md
    assert "### Internal (CAUTION)" in md
    assert "### Excluded hosts (EXCLUDED)" in md
    assert "10.0.0.0/8" in md and "10.1.1.1" in md
    assert "Kill switch" in md
    assert "Acil durdurma **talimatı**" in md


def test_project_context_md_omits_empty_collab_note() -> None:
    from grison.remote.repmap import project_context_to_md

    for empty in ("", "<p></p>", "<p> </p>"):
        project = dict(_TURKISH_PROJECT, collab_note=empty, scopes=[], whitecards=[])
        md = project_context_to_md(project)
        assert "Collab note" not in md


def test_sync_writes_project_context_mirror(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"}, project=_TURKISH_PROJECT)
    r = sync_reports(tmp_path, fake)
    ctx = _project_md(tmp_path, 6, "acme")
    assert ctx in r.materialized and ctx.exists()
    text = ctx.read_text(encoding="utf-8")
    assert "regenerated every sync" in text
    assert "Şahin Operasyonu" in text


def test_sync_pulls_two_notes_as_mirror_files(tmp_path: Path) -> None:
    project = dict(
        _TURKISH_PROJECT,
        comments=[
            {
                "id": 10,
                "note": "<p>First note here</p>",
                "timestamp": "2026-07-01",
                "operatorId": 42,
                "user": {"name": "Jane Doe", "username": "jane"},
            },
            {
                "id": 11,
                "note": "<p>Second note here</p>",
                "timestamp": "2026-07-02",
                "operatorId": 42,
                "user": {"name": None, "username": "jane"},
            },
        ],
    )
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"}, project=project)
    r = sync_reports(tmp_path, fake)

    ndir = _notes_dir(tmp_path, 6, "acme")
    files = sorted(p.name for p in ndir.glob("*.md"))
    assert files == ["10-first-note-here.md", "11-second-note-here.md"]
    for name in files:
        assert (ndir / name) in r.materialized

    first = (ndir / "10-first-note-here.md").read_text(encoding="utf-8")
    assert "note_id: 10" in first
    assert "project_id: 1" in first
    assert "author: Jane Doe" in first
    assert "First note here" in first

    second = (ndir / "11-second-note-here.md").read_text(encoding="utf-8")
    assert "author: jane" in second  # falls back to username when name is absent


def test_sync_pushes_new_local_note_and_stamps_mirror(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"}, project=_TURKISH_PROJECT)
    sync_reports(tmp_path, fake)  # materializes the report dir + empty notes/ absent yet

    ndir = _notes_dir(tmp_path, 6, "acme")
    ndir.mkdir(parents=True, exist_ok=True)
    new_note = ndir / "new-idea.md"
    new_note.write_text("A brand **new** idea\n", encoding="utf-8")

    r = sync_reports(tmp_path, fake)

    assert not new_note.exists()  # replaced by its id-stamped mirror
    assert len(fake.notes_inserted) == 1
    inserted = fake.notes_inserted[0]
    assert inserted["projectId"] == 1
    assert inserted["operatorId"] == 42  # resolved via whoami -> resolve_user_id
    assert inserted["note"] == "<p>A brand <strong>new</strong> idea</p>"

    mirrors = sorted(p.name for p in ndir.glob("*.md"))
    assert len(mirrors) == 1 and mirrors[0].startswith("100-")  # fake's first minted id
    stamped = (ndir / mirrors[0]).read_text(encoding="utf-8")
    assert "note_id: 100" in stamped
    assert "A brand **new** idea" in stamped
    assert (ndir / mirrors[0]) in r.notes_pushed


def test_sync_push_note_leaves_existing_mirrors_and_extra_fields_untouched(tmp_path: Path) -> None:
    project = dict(
        _TURKISH_PROJECT,
        comments=[
            {
                "id": 10,
                "note": "<p>Existing note</p>",
                "timestamp": "2026-07-01",
                "operatorId": 42,
                "user": {"name": "Jane Doe", "username": "jane"},
            }
        ],
    )
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"}, project=project)
    sync_reports(tmp_path, fake)  # pulls the existing note mirror

    ndir = _notes_dir(tmp_path, 6, "acme")
    existing_mirror = ndir / "10-existing-note.md"
    before = existing_mirror.read_text(encoding="utf-8")
    (ndir / "new-idea.md").write_text("Fresh thought\n", encoding="utf-8")

    r = sync_reports(tmp_path, fake)

    assert existing_mirror.read_text(encoding="utf-8") == before  # untouched
    assert len(fake.notes_inserted) == 1  # only the new-idea file was pushed
    assert len(r.notes_pushed) == 1


def test_note_push_dry_run_makes_no_insert_or_file_change(tmp_path: Path) -> None:
    fake = FakeGW()
    fake.add_report(6, "Acme", {"executive_summary": "<p>s</p>"}, project=_TURKISH_PROJECT)
    sync_reports(tmp_path, fake)

    ndir = _notes_dir(tmp_path, 6, "acme")
    ndir.mkdir(parents=True, exist_ok=True)
    new_note = ndir / "new-idea.md"
    new_note.write_text("A dry-run idea\n", encoding="utf-8")

    events: list[str] = []
    r = sync_reports(tmp_path, fake, dry_run=True, on_event=events.append)

    assert not fake.notes_inserted  # no insert happened
    assert new_note.exists()  # untouched
    assert new_note.read_text(encoding="utf-8") == "A dry-run idea\n"
    assert new_note in r.notes_pushed
    assert any("would push note" in e and "new-idea.md" in e for e in events)


def test_scope_trip_wire_flags_report_but_other_reports_still_sync(tmp_path: Path) -> None:
    no_scope_project = dict(_TURKISH_PROJECT, id=5, scopes=[])
    fake = FakeGW()
    fake.add_report(5, "NoScope", {"executive_summary": "<p>a</p>"}, project=no_scope_project)
    fake.add_report(6, "Acme", {"executive_summary": "<p>b</p>"}, project=_TURKISH_PROJECT)

    r = sync_reports(tmp_path, fake)

    assert any(
        "report 5" in msg and "no scope defined" in msg for msg in r.scope_failures
    )
    good = _sec(tmp_path, 6, "acme", "executive_summary")
    assert good in r.pulled and good.exists()
    noscope_section = _sec(tmp_path, 5, "noscope", "executive_summary")
    assert noscope_section in r.pulled and noscope_section.exists()  # sync work still proceeds
