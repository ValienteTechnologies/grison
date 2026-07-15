"""Phase-5 integration: parse a dir of mixed scanner exports into markdown."""

from __future__ import annotations

import shutil
from pathlib import Path

from grison.markdown import markdown_to_finding
from grison.sinks import run_parse

_FIX = Path(__file__).parent / "fixtures" / "scanners"
_ALL_SCANNERS = {"acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap"}


def _input_dir(tmp_path: Path) -> Path:
    inp = tmp_path / "in"
    inp.mkdir()
    for f in _FIX.iterdir():
        shutil.copy(f, inp / f.name)
    (inp / "notes.txt").write_text("just some notes, not a scan\n")
    return inp


def test_parse_dir_autodetects_all_and_skips_unknown(tmp_path: Path) -> None:
    inp = _input_dir(tmp_path)
    out = tmp_path / "inbox"
    summary = run_parse([inp], out)

    assert set(summary.files_parsed) == _ALL_SCANNERS  # every fixture auto-detected
    assert any(
        p.name == "notes.txt" and "unrecognized" in reason
        for p, reason in summary.skipped_files
    )

    md_files = sorted(out.glob("*.md"))
    assert len(md_files) == len(summary.findings) >= len(_ALL_SCANNERS)
    assert summary.sink is not None and len(summary.sink.written) == len(md_files)
    # everything written is a valid, re-parseable finding at instance tier
    for m in md_files:
        f = markdown_to_finding(m.read_text())
        assert f.grison.tier == "instance"
        assert f.grison.gw.id is None


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    inp = _input_dir(tmp_path)
    out = tmp_path / "inbox"
    first = run_parse([inp], out)
    second = run_parse([inp], out)

    assert second.sink is not None
    assert second.sink.written == []  # nothing re-written
    assert len(second.sink.unchanged) == len(first.findings)  # all identical


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    out = tmp_path / "inbox"
    summary = run_parse([_FIX / "burp_sample.xml"], out, dry_run=True)

    assert summary.sink is not None and summary.sink.written  # would-write is reported
    assert list(out.glob("*.md")) == []  # but nothing landed on disk


def test_single_file_and_min_severity(tmp_path: Path) -> None:
    out = tmp_path / "inbox"
    # filter out everything below critical — the synthetic burp finding is lower
    summary = run_parse([_FIX / "burp_sample.xml"], out, min_severity="critical")
    assert summary.files_parsed == {"burp": 1}
    assert summary.findings == []  # filtered out by severity
