"""End-to-end contract test: every fixture the golden suite
(``tests/scanners/test_golden.py``) records as detected, parsed ok, and
non-empty must survive the real pipeline — ``grison parse`` into a fresh
bootstrapped workspace, then ``grison validate`` — clean (exit 0). Same
fixture -> parse -> validate shape as
``tests/cli/test_cli.py::test_parse_in_empty_dir_then_validate_exits_clean``.

A fixture is skipped here (not this test's concern — see
``tests/scanners/test_golden.py``'s module docstring) when its golden records
it as undetected, a parse error, or zero findings: those are current,
recorded parser behaviour, not something the pipeline can turn into a
workspace document at all.

Runtime: one ``grison parse`` + one ``grison validate`` invocation per
scanner that has at least one eligible fixture, not per file, to keep this
fast.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison.cli import app

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_EXPECTED = _FIX / "expected"
_SCANNER_DIRS = ("acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap")

_runner = CliRunner()

# Fixtures excluded from the contract batch despite a clean detected+ok+findings
# golden — each hits a real, currently-existing bug OUTSIDE the parsers (markdown
# mapping / validator layer, not grison/scanners/), found by running this very
# test against the real corpus. Left unfixed per this step's scope (parsers only,
# record-don't-change); reported to the caller instead of silently working around
# it in production code. Excluding at file granularity (not dropping the whole
# scanner) keeps every other real-corpus fixture for that scanner under contract
# coverage.
#
# Each entry's value is the validator rule id ``grison validate`` currently fails
# the file on — asserted by the staleness canary below (``test_known_bug_is_still_a_bug``)
# so that when the underlying bug gets fixed, the canary fails loudly instead of
# the exclusion silently going stale (the file would then just be skipped forever,
# with no contract coverage and no signal that it could be un-excluded).
_KNOWN_PIPELINE_BUGS: dict[str, dict[str, str]] = {
    # openvas/dojo-many_vuln.xml (FND-016, embedded newlines in the NVT title
    # breaking it across lines) was fixed by the parser-convergence step that
    # added shared title whitespace-collapsing (grison.scanners.base.collapse_whitespace,
    # applied in Aggregator.add) — the canary below caught the fix and this entry
    # was dropped; the file is back under test_parse_then_validate's coverage.
    #
    # burp: this file's "Cross-site scripting (reflected)" finding description
    # contains the HTML-entity-escaped payload "&lt;script&gt;...&lt;/script&gt;"
    # (safe, literal text in the source XML). Somewhere in HTML->markdown mapping
    # it comes back out HTML-unescaped as a literal "<script>" tag, which the
    # markdown validator then rejects as unsupported inline HTML (FND-014).
    "burp": {"dojo-seven_findings.xml": "FND-014"},
}


def _eligible_files(scanner: str) -> list[str]:
    """Fixture basenames under tests/fixtures/scanners/<scanner>/ whose golden
    records detected + ok + at least one finding, minus _KNOWN_PIPELINE_BUGS."""
    names = []
    exp_dir = _EXPECTED / scanner
    if not exp_dir.is_dir():
        return names
    excluded = _KNOWN_PIPELINE_BUGS.get(scanner, {})
    for exp in sorted(exp_dir.glob("*.ir.json")):
        stem = exp.name[: -len(".ir.json")]
        if stem in excluded:
            continue
        doc = json.loads(exp.read_text())
        if doc["detected"] and doc["outcome"] == "ok" and len(doc["findings"]) >= 1:
            names.append(stem)
    return names


_SCANNERS_WITH_ELIGIBLE_FIXTURES = [s for s in _SCANNER_DIRS if _eligible_files(s)]


@pytest.mark.parametrize("scanner", _SCANNERS_WITH_ELIGIBLE_FIXTURES)
def test_parse_then_validate(scanner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = tmp_path / "scans"
    scans.mkdir()
    for name in _eligible_files(scanner):
        shutil.copy(_FIX / scanner / name, scans / name)

    monkeypatch.chdir(tmp_path)  # workspace root = tmp_path, same as the CLI tests

    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 0, r.output

    inbox_files = list((tmp_path / "findings" / "inbox").glob("*.md"))
    assert inbox_files

    r2 = _runner.invoke(app, ["validate"])
    assert r2.exit_code == 0, r2.output


_KNOWN_BUG_CASES = [
    (scanner, fname, rule_id)
    for scanner, files in _KNOWN_PIPELINE_BUGS.items()
    for fname, rule_id in files.items()
]
_KNOWN_BUG_IDS = [f"{scanner}/{fname}" for scanner, fname, _ in _KNOWN_BUG_CASES]


@pytest.mark.parametrize(("scanner", "fname", "rule_id"), _KNOWN_BUG_CASES, ids=_KNOWN_BUG_IDS)
def test_known_bug_is_still_a_bug(
    scanner: str, fname: str, rule_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staleness canary for ``_KNOWN_PIPELINE_BUGS``: each excluded file must
    still fail ``grison validate`` with the rule id recorded for it. If this
    test fails, the underlying bug has been fixed (or the file's behavior
    changed) — drop that file's entry from ``_KNOWN_PIPELINE_BUGS`` and move it
    back under ``test_parse_then_validate``'s contract coverage instead of
    leaving it silently excluded."""
    scans = tmp_path / "scans"
    scans.mkdir()
    shutil.copy(_FIX / scanner / fname, scans / fname)

    monkeypatch.chdir(tmp_path)

    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 0, r.output

    r2 = _runner.invoke(app, ["validate", "--json"])
    assert r2.exit_code != 0, (
        f"{scanner}/{fname} now passes validate — its _KNOWN_PIPELINE_BUGS entry "
        "is stale, drop it and re-include the file in the contract batch"
    )
    failures = json.loads(r2.output)
    rule_ids = [f["rule_id"] for f in failures]
    assert rule_id in rule_ids, (
        f"{scanner}/{fname} still fails validate, but no longer with {rule_id} "
        f"(got {rule_ids}) — its _KNOWN_PIPELINE_BUGS entry is stale, update or drop it"
    )
