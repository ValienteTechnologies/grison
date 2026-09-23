"""Nmap parser test (synthetic fixture).

Nmap is recon output (open ports), not findings — grison.scanners.nmap.NmapScanner
refuses every input (RefusedInput; see grison/scanners/base.py's docstring for the
category). Detection is unaffected: an nmap export still sniffs as "nmap" so the
refusal message stays specific instead of falling back to "unrecognized scanner
type" (see tests/scanners/test_detect.py for detection coverage)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.scanners import ImportOptions, NmapScanner, RefusedInput, detect
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanners"

_REFUSAL = "nmap is recon output, not findings; inventory support is pending"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_nmap_sample_still_detects_as_nmap() -> None:
    assert detect(FIXTURES / "nmap/nmap_sample.xml") == "nmap"


def test_parse_refuses_xml_input() -> None:
    with pytest.raises(RefusedInput, match=_REFUSAL):
        NmapScanner().parse(load("nmap/nmap_sample.xml"), ImportOptions())


def test_parse_refuses_regardless_of_options() -> None:
    # The refusal doesn't depend on severity filtering or any other option —
    # every input is refused, unconditionally.
    opts = ImportOptions(severity_filter={Severity.CRITICAL})
    with pytest.raises(RefusedInput, match=_REFUSAL):
        NmapScanner().parse(load("nmap/nmap_sample.xml"), opts)


def test_parse_refuses_even_malformed_or_empty_input() -> None:
    # The refusal fires before any XML is touched — it's not "invalid nmap XML",
    # it's "nmap output is out of scope regardless of its validity".
    with pytest.raises(RefusedInput, match=_REFUSAL):
        NmapScanner().parse(b"not even xml", ImportOptions())
    with pytest.raises(RefusedInput, match=_REFUSAL):
        NmapScanner().parse(b"", ImportOptions())
