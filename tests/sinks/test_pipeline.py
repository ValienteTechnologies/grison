"""Phase-5 integration: parse a dir of mixed scanner exports into markdown."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from grison.formats import finding as finding_fmt
from grison.sinks import ParsePathNotFound, run_parse

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_ALL_SCANNERS = {"acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap"}

# One hand-made sample per scanner (not the whole vendored corpus — this test is
# about mixed-directory auto-detection, not corpus coverage; see
# tests/scanners/test_golden.py and tests/scanners/test_contract.py for that).
_ONE_SAMPLE_PER_SCANNER = (
    "acunetix/acunetix_sample.xml",
    "burp/burp_sample.xml",
    "nessus/nessus_sample.xml",
    "nmap/nmap_sample.xml",
    "openvas/openvas_sample.xml",
    "qualys/qualys_sample.xml",
    "sslyze/sslyze_sample.json",
    "zap/zap_sample.xml",
)


def _input_dir(tmp_path: Path) -> Path:
    inp = tmp_path / "in"
    inp.mkdir()
    for rel in _ONE_SAMPLE_PER_SCANNER:
        f = _FIX / rel
        shutil.copy(f, inp / f.name)
    (inp / "notes.txt").write_text("just some notes, not a scan\n")
    return inp


def _out_dir(tmp_path: Path) -> Path:
    # under findings/inbox/ so grison.formats.finding.tier_of() derives "inbox" —
    # the real shape grison parse always writes to (BRIEF workspace layout).
    return tmp_path / "findings" / "inbox"


def test_parse_dir_autodetects_all_and_skips_unknown(tmp_path: Path) -> None:
    inp = _input_dir(tmp_path)
    out = _out_dir(tmp_path)
    summary = run_parse([inp], out)

    assert set(summary.files_parsed) == _ALL_SCANNERS  # every fixture auto-detected
    assert any(
        p.name == "notes.txt" and "unrecognized" in reason for p, reason in summary.skipped_files
    )

    md_files = sorted(out.glob("*.md"))
    assert len(md_files) == len(summary.findings) >= len(_ALL_SCANNERS)
    assert summary.sink is not None and len(summary.sink.written) == len(md_files)
    # everything written is a valid, re-parseable inbox finding with no machine field
    for m in md_files:
        text = m.read_text()
        assert "grison:" not in text
        finding_fmt.parse(text, path=m)  # raises FormatError if this isn't a clean v2 doc


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    inp = _input_dir(tmp_path)
    out = _out_dir(tmp_path)
    first = run_parse([inp], out)
    second = run_parse([inp], out)

    assert second.sink is not None
    assert second.sink.written == []  # nothing re-written
    assert len(second.sink.unchanged) == len(first.findings)  # all identical


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    out = _out_dir(tmp_path)
    summary = run_parse([_FIX / "burp/burp_sample.xml"], out, dry_run=True)

    assert summary.sink is not None and summary.sink.written  # would-write is reported
    assert list(out.glob("*.md")) == []  # but nothing landed on disk


def test_single_file_and_min_severity(tmp_path: Path) -> None:
    out = _out_dir(tmp_path)
    # filter out everything below critical — the synthetic burp finding is lower
    summary = run_parse([_FIX / "burp/burp_sample.xml"], out, min_severity="critical")
    assert summary.files_parsed == {"burp": 1}
    assert summary.findings == []  # filtered out by severity
    assert summary.errors == []  # zero findings from a recognized file is not a failure


def test_unrecognized_file_is_recorded_as_an_error_too(tmp_path: Path) -> None:
    # Bug fix: an unrecognized file used to only land in `skipped_files`, never
    # `errors` — the CLI only looks at `errors` to decide the exit code, so `grison
    # parse` exited 0 having silently done nothing with it.
    out = _out_dir(tmp_path)
    notes = tmp_path / "notes.txt"
    notes.write_text("not a scan\n")
    summary = run_parse([notes], out)
    assert any("notes.txt" in e and "unrecognized" in e for e in summary.errors)


def test_missing_path_raises_before_any_file_is_touched(tmp_path: Path) -> None:
    # Bug fix: a nonexistent path argument used to be silently added to
    # `skipped_files` and the run proceeded (and exited 0) — it must instead refuse
    # to run at all, so a typo'd path never comes back as a quiet success.
    out = _out_dir(tmp_path)
    good = _FIX / "burp/burp_sample.xml"
    missing = tmp_path / "no-such-file.xml"

    with pytest.raises(ParsePathNotFound, match="no-such-file.xml"):
        run_parse([good, missing], out)

    assert not out.exists()  # not even the valid path was processed
