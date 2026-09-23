"""Malformed-input hardening: detection and parsing must never raise past the
pipeline's own error bookkeeping (``grison.sinks.pipeline.run_parse``), no
matter how a scanner export gets mangled — truncation, a flipped byte, or an
empty file.

For every fixture the golden suite (``tests/scanners/test_golden.py``) records
as detected + parsed ok, this derives four corrupt variants *in test* (no new
fixture files committed):

- truncated at 50% of its byte length
- truncated to its first 200 bytes
- emptied out entirely
- one byte flipped inside its first XML/JSON tag

Each variant is run through ``run_parse`` — the same entry point ``grison
parse`` uses — and the only two outcomes allowed are: it still parses (the
corruption happened to land somewhere that doesn't matter, e.g. a truncated
tail of an otherwise well-formed document), or it fails and that failure is
recorded in ``summary.errors`` (never a bare skip, never an exception
escaping). One representative case is also driven through the real CLI to
confirm the non-zero exit code reaches that layer too.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison.cli import app
from grison.scanners.detect import _HEAD_BYTES, InputEncodingError, detect_bytes, normalise_input
from grison.sinks import run_parse

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_EXPECTED = _FIX / "expected"
_SCANNER_DIRS = ("acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap")

_SEED = 20260923  # fixed, for deterministic byte flips

_runner = CliRunner()


def _eligible_fixtures() -> list[Path]:
    """Every fixture whose golden records detected + parsed ok (any finding
    count) — the ones a malformed-input test should expect to have *started*
    from working input."""
    fixtures: list[Path] = []
    for scanner in _SCANNER_DIRS:
        exp_dir = _EXPECTED / scanner
        if not exp_dir.is_dir():
            continue
        for exp in sorted(exp_dir.glob("*.ir.json")):
            doc = json.loads(exp.read_text())
            if doc["detected"] and doc["outcome"] == "ok":
                fixtures.append(_FIX / scanner / exp.name[: -len(".ir.json")])
    return fixtures


def _truncate_half(data: bytes) -> bytes:
    return data[: len(data) // 2]


def _truncate_200(data: bytes) -> bytes:
    return data[:200]


def _empty(data: bytes) -> bytes:  # noqa: ARG001 — same signature as the other derivers
    return b""


def _flip_byte_in_first_tag(data: bytes) -> bytes:
    """Flip one bit, at a fixed-seed pseudo-random position, inside the file's
    first '<...>' tag (or its first ~50 bytes, for JSON/malformed input with no
    tag at all)."""
    start = data.find(b"<")
    if start == -1:
        start = 0
    end = data.find(b">", start)
    if end == -1:
        end = min(len(data) - 1, start + 50)
    if end < start or not data:
        return data
    pos = random.Random(_SEED).randint(start, end)
    pos = min(pos, len(data) - 1)
    mutated = bytearray(data)
    mutated[pos] ^= 0xFF
    return bytes(mutated)


_MUTATIONS = {
    "truncated-50pct": _truncate_half,
    "truncated-200b": _truncate_200,
    "empty": _empty,
    "byte-flip-first-tag": _flip_byte_in_first_tag,
}

_FIXTURES = _eligible_fixtures()
_CASES = [(f, name) for f in _FIXTURES for name in _MUTATIONS]
_IDS = [f"{f.parent.name}/{f.name}::{name}" for f, name in _CASES]


@pytest.mark.parametrize(("fixture", "mutation"), _CASES, ids=_IDS)
def test_malformed_variant_never_escapes_the_pipeline(
    fixture: Path, mutation: str, tmp_path: Path
) -> None:
    data = _MUTATIONS[mutation](fixture.read_bytes())
    mangled = tmp_path / "in" / fixture.name
    mangled.parent.mkdir(parents=True, exist_ok=True)
    mangled.write_bytes(data)

    # The point of this test: this call must never raise. If it does, that's an
    # uncaught exception escaping run_parse's own try/except bookkeeping — a bug
    # in the pipeline, not in the fixture.
    summary = run_parse([mangled], tmp_path / "out")

    # Every skip must be paired with an `errors` entry (this is the same
    # invariant tests/sinks/test_pipeline.py checks for the unrecognized-file
    # and parser-raises cases individually; here it's checked across a whole
    # corpus of corruptions at once).
    for path, _reason in summary.skipped_files:
        assert any(path.name in e for e in summary.errors), (
            f"{path.name} was skipped but has no matching entry in errors (mutation={mutation})"
        )


def test_empty_file_exits_nonzero_at_cli_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One representative malformed case driven through the real CLI (not just
    # run_parse directly) to confirm the non-zero exit code reaches that layer.
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / (_FIXTURES[0].name)).write_bytes(b"")
    monkeypatch.chdir(tmp_path)

    r = _runner.invoke(app, ["parse", str(scans)])

    assert r.exit_code == 1
    assert _FIXTURES[0].name in r.output


@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f"{f.parent.name}/{f.name}" for f in _FIXTURES])
def test_50pct_truncation_never_yields_outcome_ok(fixture: Path, tmp_path: Path) -> None:
    # A 50%-truncated variant of a fixture that started out detected + parsed ok
    # must never come back "ok" itself — half of a well-formed XML/JSON document
    # is, by construction, not itself well-formed (an unclosed tag/bracket), so
    # this must always land in `file_errors`, never quietly succeed with a
    # partial or empty finding list.
    data = _truncate_half(fixture.read_bytes())
    mangled = tmp_path / "in" / fixture.name
    mangled.parent.mkdir(parents=True, exist_ok=True)
    mangled.write_bytes(data)

    summary = run_parse([mangled], tmp_path / "out")

    assert not summary.files_parsed  # never counted as a successfully parsed file
    assert any(mangled.name in e for e in summary.file_errors)


def test_corpus_wide_error_count_covers_every_detected_mutation(tmp_path: Path) -> None:
    # Across the WHOLE mutated corpus (every fixture x every mutation, one
    # combined run): any mutated file that still sniffs as a known scanner type
    # must not be silently accepted — the error count must be at least the
    # count of mutated files that were still detected. A mutation that happens
    # to land somewhere harmless (still detected, still parses fine) is allowed
    # and simply isn't required to contribute an error of its own; what's
    # disallowed is detected-but-corrupted files outnumbering the errors
    # recorded for them.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    detected_count = 0
    for fixture in _FIXTURES:
        raw = fixture.read_bytes()
        for mname, mutate in _MUTATIONS.items():
            mutated = mutate(raw)
            try:
                normalised = normalise_input(mutated)
            except InputEncodingError:
                normalised = None
            if normalised is not None and detect_bytes(normalised[:_HEAD_BYTES]) is not None:
                detected_count += 1
            unique = in_dir / f"{fixture.parent.name}__{fixture.name}__{mname}"
            unique.write_bytes(mutated)

    summary = run_parse([in_dir], tmp_path / "out")

    assert len(summary.errors) >= detected_count


def test_empty_variant_is_always_unrecognized(tmp_path: Path) -> None:
    # The empty-file mutation, run across the whole corpus in one batch: every
    # single one must be unrecognized (never accidentally detected from zero
    # bytes), and none of them may land in `finding_errors` — an empty file is
    # a file-level failure, not a finding that failed validation.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    names = []
    for fixture in _FIXTURES:
        name = f"{fixture.parent.name}__{fixture.name}"
        (in_dir / name).write_bytes(b"")
        names.append(name)

    summary = run_parse([in_dir], tmp_path / "out")

    reasons = dict(((p.name, reason) for p, reason in summary.skipped_files))
    for name in names:
        assert reasons.get(name) == "unrecognized scanner type", name
    assert summary.finding_errors == []
