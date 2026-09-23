"""Sniffer tests — each scanner's native root maps to its slug; non-matches skip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grison.scanners import BY_NAME, detect, detect_bytes, scanner_for

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_EXPECTED = _FIX / "expected"
_SCANNER_DIRS = ("acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap")


@pytest.mark.parametrize(
    ("fname", "expected"),
    [
        ("acunetix/acunetix_sample.xml", "acunetix"),
        ("burp/burp_sample.xml", "burp"),
        ("nessus/nessus_sample.xml", "nessus"),
        ("nmap/nmap_sample.xml", "nmap"),
        ("openvas/openvas_sample.xml", "openvas"),
        ("qualys/qualys_sample.xml", "qualys"),
        ("sslyze/sslyze_sample.json", "sslyze"),
        ("zap/zap_sample.xml", "zap"),
    ],
)
def test_detect_each_fixture(fname: str, expected: str) -> None:
    assert detect(_FIX / fname) == expected
    # and the slug resolves back to a real parser class
    assert scanner_for(expected) is BY_NAME[expected]


def test_acunetix_scan_root_variant() -> None:
    # Acunetix's inner <Scan> root is also accepted.
    assert detect_bytes(b"<Scan><StartURL>x</StartURL></Scan>") == "acunetix"


def test_qualys_was_root_variant() -> None:
    assert detect_bytes(b"<WAS_SCAN_REPORT></WAS_SCAN_REPORT>") == "qualys"


def test_unknown_xml_root_is_none() -> None:
    assert detect_bytes(b"<foobar><child/></foobar>") is None


def test_non_scanner_input_is_none() -> None:
    assert detect_bytes(b"just some text, not a scan") is None
    assert detect_bytes(b"") is None
    assert detect_bytes(b"   \n\t  ") is None


def test_json_without_markers_is_none() -> None:
    assert detect_bytes(b'{"hello": "world"}') is None


def test_scanner_for_unknown_is_none() -> None:
    assert scanner_for("nope") is None


def _corpus_detect_cases() -> list[tuple[Path, str | None]]:
    """(fixture path, expected slug or None) for every corpus fixture, expected
    value taken from its golden (tests/scanners/test_golden.py) — this test
    doesn't re-derive current behaviour, it just asserts detection matches what
    the golden already recorded, with a message that names the file plainly
    when detection regresses (the golden's diff output names the whole JSON
    document instead)."""
    cases: list[tuple[Path, str | None]] = []
    for scanner in _SCANNER_DIRS:
        exp_dir = _EXPECTED / scanner
        if not exp_dir.is_dir():
            continue
        for exp in sorted(exp_dir.glob("*.ir.json")):
            fixture = _FIX / scanner / exp.name[: -len(".ir.json")]
            expected = json.loads(exp.read_text())["detected"]
            cases.append((fixture, expected))
    return cases


_CORPUS_CASES = _corpus_detect_cases()
_CORPUS_IDS = [f"{p.parent.name}/{p.name}" for p, _ in _CORPUS_CASES]


@pytest.mark.parametrize(("fixture", "expected"), _CORPUS_CASES, ids=_CORPUS_IDS)
def test_detect_matches_golden_for_every_corpus_fixture(
    fixture: Path, expected: str | None
) -> None:
    assert detect(fixture) == expected
