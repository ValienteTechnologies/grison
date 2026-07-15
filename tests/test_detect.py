"""Sniffer tests — each scanner's native root maps to its slug; non-matches skip."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.scanners import BY_NAME, detect, detect_bytes, scanner_for

_FIX = Path(__file__).parent / "fixtures" / "scanners"


@pytest.mark.parametrize(
    ("fname", "expected"),
    [
        ("acunetix_sample.xml", "acunetix"),
        ("burp_sample.xml", "burp"),
        ("nessus_sample.xml", "nessus"),
        ("nmap_sample.xml", "nmap"),
        ("openvas_sample.xml", "openvas"),
        ("qualys_sample.xml", "qualys"),
        ("sslyze_sample.json", "sslyze"),
        ("zap_sample.xml", "zap"),
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
