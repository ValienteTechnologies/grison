"""Phase-7 pull tests with a fake Ghostwriter client (no live calls)."""

from __future__ import annotations

from pathlib import Path

from grison.markdown import markdown_to_finding
from grison.remote.sync import pull

_LIB = [
    {
        "id": 1,
        "title": "Lib A",
        "severityId": 3,
        "findingTypeId": 4,
        "cvssVector": "",
        "cvssScore": None,
        "description": "<p>alpha</p>",
        "impact": "",
        "mitigation": "",
        "references": "",
        "replication_steps": "",
    }
]
_RF = [
    {
        "id": 10,
        "reportId": 7,
        "title": "Inst A",
        "severityId": 4,
        "findingTypeId": 4,
        "cvssVector": "",
        "cvssScore": None,
        "description": "<p>beta</p>",
        "impact": "",
        "mitigation": "",
        "references": "",
        "replication_steps": "",
        "affectedEntities": "<p>192.0.2.9</p>",
    }
]
_EV = [{"id": 99, "findingId": 10, "reportId": None, "document": "evidence/9/shot.png",
        "caption": "c", "friendlyName": "shot"}]
_REPORTS = [{"id": 7, "title": "Acme"}]
_IMG = b"\x89PNG\r\n\x1a\n-fake-png-bytes"


class FakeGW:
    def __init__(self, findings=None):
        self._findings = findings if findings is not None else _LIB

    def fetch_findings(self):
        return self._findings

    def fetch_reported_findings(self):
        return _RF

    def fetch_evidence(self):
        return _EV

    def fetch_reports(self):
        return _REPORTS

    def fetch_tag_map(self):
        return {}

    def download_evidence(self, evidence_id: int):
        return ("shot.png", _IMG)


def test_pull_mirrors_and_is_idempotent(tmp_path: Path) -> None:
    r1 = pull(tmp_path, FakeGW())
    assert len(r1.written) == 2 and r1.evidence_written == 1
    libf = tmp_path / "findings" / "library" / "lib-a.md"
    assert libf.exists()
    rf_files = list((tmp_path / "findings" / "reports").rglob("*.md"))
    assert len(rf_files) == 1 and rf_files[0].name == "10-inst-a.md"
    img = tmp_path / "findings" / "reports" / "7-acme" / "evidence" / "shot.png"
    assert img.exists() and img.read_bytes() == _IMG

    f = markdown_to_finding(libf.read_text())
    assert f.grison.synced is not None and f.grison.synced.hash  # merge base stamped
    assert f.evidence == []

    r2 = pull(tmp_path, FakeGW())  # re-pull is a no-op
    assert r2.written == [] and len(r2.unchanged) == 2


def test_pull_preserves_local_edit(tmp_path: Path) -> None:
    pull(tmp_path, FakeGW())
    libf = tmp_path / "findings" / "library" / "lib-a.md"
    libf.write_text(libf.read_text().replace("Lib A", "Lib A EDITED"))
    r = pull(tmp_path, FakeGW())
    assert libf in r.local_ahead  # not clobbered
    assert "EDITED" in libf.read_text()


def test_pull_fast_forwards_clean_local(tmp_path: Path) -> None:
    pull(tmp_path, FakeGW())
    changed = [{**_LIB[0], "description": "<p>alpha CHANGED</p>"}]
    r = pull(tmp_path, FakeGW(findings=changed))
    libf = tmp_path / "findings" / "library" / "lib-a.md"
    assert libf in r.written
    assert "CHANGED" in libf.read_text()


def test_pull_detects_collision(tmp_path: Path) -> None:
    pull(tmp_path, FakeGW())
    libf = tmp_path / "findings" / "library" / "lib-a.md"
    libf.write_text(libf.read_text().replace("alpha", "alpha LOCAL"))
    changed = [{**_LIB[0], "description": "<p>alpha REMOTE</p>"}]
    r = pull(tmp_path, FakeGW(findings=changed))
    assert libf in r.collisions
    assert "LOCAL" in libf.read_text()  # collision never overwrites local


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    r = pull(tmp_path, FakeGW(), dry_run=True)
    assert len(r.written) == 2  # would-write reported
    assert not (tmp_path / "findings" / "library" / "lib-a.md").exists()


def test_pull_emits_progress_events(tmp_path: Path) -> None:
    events: list[str] = []
    r = pull(tmp_path, FakeGW(), on_event=events.append)
    assert len(r.written) == 2
    assert any("pulling remote state from ghostwriter" in e for e in events)
    assert any(e.startswith("remote: ") for e in events)
    libf = tmp_path / "findings" / "library" / "lib-a.md"
    assert f"pull {libf.relative_to(tmp_path)}" in events


def test_pull_dry_run_emits_would_prefixed_events(tmp_path: Path) -> None:
    events: list[str] = []
    r = pull(tmp_path, FakeGW(), dry_run=True, on_event=events.append)
    assert len(r.written) == 2
    assert any(e.startswith("would pull ") for e in events)


def test_evidence_basename_collision_across_findings_disambiguates(tmp_path: Path) -> None:
    """Two findings in one report each carrying a 'shell.png' must map to distinct
    local paths — a per-finding de-dup would let the second download silently
    overwrite the first's image in the shared evidence/ directory."""
    rf = [dict(_RF[0]), {**_RF[0], "id": 11, "title": "Inst B"}]
    ev = [
        {"id": 99, "findingId": 10, "reportId": None, "document": "evidence/9/shell.png",
         "caption": "a", "friendlyName": "shell-a"},
        {"id": 100, "findingId": 11, "reportId": None, "document": "evidence/12/shell.png",
         "caption": "b", "friendlyName": "shell-b"},
    ]

    class CollidingGW(FakeGW):
        def fetch_reported_findings(self):
            return rf

        def fetch_evidence(self):
            return ev

    r = pull(tmp_path, CollidingGW())
    assert r.evidence_written == 2
    ev_dir = tmp_path / "findings" / "reports" / "7-acme" / "evidence"
    assert (ev_dir / "99-shell.png").exists()
    assert (ev_dir / "100-shell.png").exists()
    for md in (tmp_path / "findings" / "reports").rglob("*.md"):
        f = markdown_to_finding(md.read_text())
        assert len(f.evidence) == 1
        assert f.evidence[0].file.startswith("evidence/") and "-shell.png" in f.evidence[0].file


def test_pull_preserves_locally_owned_evidence_metadata(tmp_path: Path) -> None:
    """Evidence caption/friendly_name (no GW update-in-place path until Track 1b) are
    locally owned — a routine pull triggered by an unrelated remote edit must carry
    them forward, not rebuild the file without them. cwe/tags are NOT locally-owned
    anymore (they sync two-way via GW's tag mechanism — see test_sync_tags.py); an
    edit to either now makes local diverge from the merge base like any other
    content field, so it is intentionally excluded from this fixture."""
    from grison.markdown import finding_to_markdown

    pull(tmp_path, FakeGW())
    rf_file = next((tmp_path / "findings" / "reports").rglob("*.md"))
    f = markdown_to_finding(rf_file.read_text())
    f.evidence[0].caption = "EDITED CAPTION"
    f.evidence[0].friendly_name = "edited-name"
    rf_file.write_text(finding_to_markdown(f))

    class ChangedGW(FakeGW):
        def fetch_reported_findings(self):
            return [{**_RF[0], "description": "<p>beta CHANGED</p>"}]

    r = pull(tmp_path, ChangedGW())
    assert rf_file in r.written
    f2 = markdown_to_finding(rf_file.read_text())
    assert "CHANGED" in f2.description  # the remote edit arrived
    assert f2.evidence[0].caption == "EDITED CAPTION"
    assert f2.evidence[0].friendly_name == "edited-name"


def test_duplicate_library_titles_get_distinct_files(tmp_path: Path) -> None:
    """Two GW library findings sharing a title must not collapse onto one local
    file (silent last-writer-wins, recurring every sync)."""
    libs = [dict(_LIB[0]), {**_LIB[0], "id": 2, "description": "<p>other body</p>"}]
    pull(tmp_path, FakeGW(findings=libs))
    lib_dir = tmp_path / "findings" / "library"
    assert (lib_dir / "1-lib-a.md").exists()
    assert (lib_dir / "2-lib-a.md").exists()
    r2 = pull(tmp_path, FakeGW(findings=libs))  # stable across re-pulls
    assert r2.written == [] and len(r2.unchanged) == 3
