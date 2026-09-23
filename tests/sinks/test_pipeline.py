"""Phase-5 integration: parse a dir of mixed scanner exports into markdown."""

from __future__ import annotations

import os
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

    # nmap is recon output and is refused (RefusedInput), not parsed — every
    # other scanner still auto-detects and parses.
    assert set(summary.files_parsed) == _ALL_SCANNERS - {"nmap"}
    assert any(
        p.name == "nmap_sample.xml" and "recon output" in msg for p, msg in summary.refused_files
    )
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


def test_detected_file_whose_parser_raises_is_recorded_as_an_error(tmp_path: Path) -> None:
    # Bug fix: a detected file whose parser raises used to land only in
    # `skipped_files`, never `errors` — the CLI only looks at `errors` to decide
    # the exit code, so `grison parse` exited 0 having silently discarded a
    # broken scan file. Root tag alone ("SCAN") is enough for detect_bytes to
    # recognize this as qualys without needing the rest of the document to be
    # well-formed, but the qualys parser fully re-parses the XML and blows up on
    # the truncated, unclosed <VULN> tag below.
    out = _out_dir(tmp_path)
    bad = tmp_path / "broken.xml"
    bad.write_bytes(b'<SCAN><VULN_LIST><VULN number="1"')

    summary = run_parse([bad], out)

    assert any(
        p.name == "broken.xml" and "parse error" in reason for p, reason in summary.skipped_files
    )
    assert any("broken.xml" in e and "parse error" in e for e in summary.errors)
    assert "ParseError" in "".join(summary.errors)  # exception type recorded, not swallowed


def test_refused_file_is_recorded_separately_from_a_parse_error(tmp_path: Path) -> None:
    # A RefusedInput (grison.scanners.base.RefusedInput) is a third file-level
    # outcome, distinct from both "unrecognized" and "parser raised": the file
    # WAS recognized and its parser made a deliberate call not to process it
    # (nmap is recon output, not findings). It must still fail the batch (an
    # entry in `errors`, same as any other file-level failure) but land in its
    # own `refused_files` list, not `skipped_files`/treated as a parse error.
    out = _out_dir(tmp_path)
    nmap_file = tmp_path / "nmap_sample.xml"
    shutil.copy(_FIX / "nmap/nmap_sample.xml", nmap_file)

    summary = run_parse([nmap_file], out)

    assert summary.refused_files == [
        (nmap_file, "nmap is recon output, not findings; inventory support is pending")
    ]
    assert summary.skipped_files == []  # refused, not skipped
    assert "nmap" not in summary.files_parsed  # never counted as successfully parsed
    assert any("nmap_sample.xml" in e and "recon output" in e for e in summary.file_errors)
    assert summary.errors  # still fails the batch, exit code must reflect it


def test_other_files_in_the_same_run_still_get_processed(tmp_path: Path) -> None:
    # One bad file must not kill the batch: a broken qualys file alongside a
    # good burp file still yields the burp finding.
    out = _out_dir(tmp_path)
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "broken.xml").write_bytes(b'<SCAN><VULN_LIST><VULN number="1"')
    shutil.copy(_FIX / "burp/burp_sample.xml", inp / "burp_sample.xml")

    summary = run_parse([inp], out)

    assert summary.files_parsed == {"burp": 1}
    assert summary.findings  # the good file's findings still landed
    assert any("broken.xml" in e for e in summary.errors)


def test_unrecognized_file_is_recorded_as_an_error_too(tmp_path: Path) -> None:
    # Bug fix: an unrecognized file used to only land in `skipped_files`, never
    # `errors` — the CLI only looks at `errors` to decide the exit code, so `grison
    # parse` exited 0 having silently done nothing with it.
    out = _out_dir(tmp_path)
    notes = tmp_path / "notes.txt"
    notes.write_text("not a scan\n")
    summary = run_parse([notes], out)
    assert any("notes.txt" in e and "unrecognized" in e for e in summary.errors)


def test_invalid_utf16_is_recorded_as_an_error(tmp_path: Path) -> None:
    # A UTF-16-BOM'd file whose bytes don't actually decode as UTF-16 (a lone
    # surrogate here) must be skipped with an errors entry, like the OSError
    # read-failure path, not raise out of run_parse.
    out = _out_dir(tmp_path)
    bad = tmp_path / "bad-utf16.xml"
    bad.write_bytes(b"\xfe\xff\xd8\x00" + "<SCAN></SCAN>".encode("utf-16-be"))

    summary = run_parse([bad], out)

    assert any(
        p.name == "bad-utf16.xml" and "invalid UTF-16" in reason
        for p, reason in summary.skipped_files
    )
    assert any("bad-utf16.xml" in e and "invalid UTF-16" in e for e in summary.file_errors)


def test_odd_length_utf16_buffer_is_recorded_as_an_error(tmp_path: Path) -> None:
    out = _out_dir(tmp_path)
    bad = tmp_path / "bad-utf16-odd.xml"
    bad.write_bytes(b"\xff\xfe" + "<SCAN></SCAN>".encode("utf-16-le") + b"\x41")

    summary = run_parse([bad], out)

    assert any("bad-utf16-odd.xml" in e and "invalid UTF-16" in e for e in summary.file_errors)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_unreadable_file_is_recorded_as_a_read_error(tmp_path: Path) -> None:
    out = _out_dir(tmp_path)
    unreadable = tmp_path / "unreadable.xml"
    unreadable.write_bytes(b"<SCAN></SCAN>")
    unreadable.chmod(0o000)
    try:
        summary = run_parse([unreadable], out)
    finally:
        unreadable.chmod(0o644)  # restore so tmp_path cleanup can remove it

    assert any(
        p.name == "unreadable.xml" and "read error" in reason for p, reason in summary.skipped_files
    )
    assert any("unreadable.xml" in e and "read error" in e for e in summary.file_errors)
    assert summary.finding_errors == []


def test_finding_validation_failure_is_kept_apart_from_file_level_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A finding-level failure (the file parsed fine, but one of its findings
    # failed FindingDoc validation) must land in `finding_errors`, never
    # `file_errors` — the CLI (grison.cli.render) prints the two under separate
    # headers, and conflating them would mislabel a perfectly-parsed file as
    # "could not be parsed".
    from grison.formats.finding import FindingDoc
    from grison.sinks import pipeline as pipeline_mod

    def _always_fails_validation(ir, *, finding_type, tier="inbox"):  # noqa: ARG001
        FindingDoc.model_validate({}, context={"tier": tier})  # raises: missing fields
        raise AssertionError("unreachable")

    monkeypatch.setattr(pipeline_mod, "ir_to_finding", _always_fails_validation)

    out = _out_dir(tmp_path)
    good = _FIX / "burp/burp_sample.xml"

    summary = run_parse([good], out)

    assert summary.files_parsed == {"burp": 1}  # the file itself parsed fine
    assert summary.file_errors == []
    assert summary.finding_errors  # but its finding(s) failed validation
    assert summary.errors == summary.finding_errors  # combined list still carries it


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
