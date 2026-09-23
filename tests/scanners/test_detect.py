"""Sniffer tests — each scanner's native root maps to its slug; non-matches skip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grison.scanners import BY_NAME, detect, detect_bytes, scanner_for
from grison.scanners.detect import InputEncodingError, normalise_input

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


# --- generic-root tiebreak (report/results -> openvas, issues -> burp) -------


def test_plain_report_without_openvas_markers_is_none() -> None:
    assert detect_bytes(b"<report><foo/></report>") is None


def test_plain_issues_without_burp_version_is_none() -> None:
    assert detect_bytes(b"<issues/>") is None
    assert detect_bytes(b"<issues><issue/></issues>") is None


def test_report_with_id_attribute_is_openvas() -> None:
    assert detect_bytes(b'<report id="abc"><owner/></report>') == "openvas"


def test_report_with_nested_results_child_is_openvas() -> None:
    # openvas_sample.xml's shape: a bare outer <report> wrapping an inner
    # <report><results>... — no attributes on the outer root at all.
    assert detect_bytes(b"<report><report><results/></report></report>") == "openvas"


def test_issues_with_burp_version_is_burp() -> None:
    assert detect_bytes(b'<issues burpVersion="1.0"><issue/></issues>') == "burp"


def test_every_openvas_and_burp_corpus_fixture_still_detects() -> None:
    for d, expected in (("openvas", "openvas"), ("burp", "burp")):
        fixdir = _FIX / d
        files = sorted(p for p in fixdir.iterdir() if p.is_file())
        assert files, f"no fixtures found under {fixdir}"
        for f in files:
            assert detect(f) == expected, f


def test_non_scanner_input_is_none() -> None:
    assert detect_bytes(b"just some text, not a scan") is None
    assert detect_bytes(b"") is None
    assert detect_bytes(b"   \n\t  ") is None


def test_json_without_markers_is_none() -> None:
    assert detect_bytes(b'{"hello": "world"}') is None


def test_scanner_for_unknown_is_none() -> None:
    assert scanner_for("nope") is None


# --- leading whitespace ------------------------------------------------------


def test_leading_whitespace_before_xml_declaration_still_detects() -> None:
    # Bug fix: detect_bytes lstripped only to sniff the first byte, then passed
    # the UNstripped bytes to _detect_xml, so whitespace before the root (a
    # stray leading blank line, as in the reptor Qualys corpus) made the file
    # undetected even though its root element is perfectly recognizable.
    assert detect_bytes(b'\n<?xml version="1.0"?>\n<SCAN></SCAN>') == "qualys"
    assert detect_bytes(b"  \n\t<WAS_SCAN_REPORT></WAS_SCAN_REPORT>") == "qualys"


def test_leading_whitespace_before_json_still_detects() -> None:
    assert detect_bytes(b'\n  {"server_scan_results": []}') == "sslyze"


# --- BOM / UTF-16 -------------------------------------------------------------


def _utf16(data: bytes, *, big_endian: bool) -> bytes:
    text = data.decode("utf-8")
    return text.encode("utf-16-be" if big_endian else "utf-16-le")


def test_normalise_input_strips_utf8_bom() -> None:
    data = b"\xef\xbb\xbf<SCAN></SCAN>"
    assert normalise_input(data) == b"<SCAN></SCAN>"
    assert detect_bytes(data) == "qualys"


def test_normalise_input_transcodes_utf16_le_with_bom() -> None:
    payload = b"<SCAN></SCAN>"
    data = b"\xff\xfe" + _utf16(payload, big_endian=False)
    assert normalise_input(data) == payload
    assert detect_bytes(data) == "qualys"


def test_normalise_input_transcodes_utf16_be_with_bom() -> None:
    payload = b"<SCAN></SCAN>"
    data = b"\xfe\xff" + _utf16(payload, big_endian=True)
    assert normalise_input(data) == payload
    assert detect_bytes(data) == "qualys"


def test_normalise_input_rejects_lone_surrogate_strictly() -> None:
    # A lone (unpaired) UTF-16 surrogate is a hard corruption signal — strict
    # decoding must raise, not paper over it with U+FFFD substitution.
    data = b"\xfe\xff\xd8\x00" + _utf16(b"<SCAN></SCAN>", big_endian=True)
    with pytest.raises(InputEncodingError):
        normalise_input(data)


def test_normalise_input_rejects_odd_length_utf16_buffer() -> None:
    data = b"\xff\xfe" + _utf16(b"<SCAN></SCAN>", big_endian=False) + b"\x41"
    with pytest.raises(InputEncodingError):
        normalise_input(data)


def test_normalise_input_utf16_turkish_chars_match_utf8_twin() -> None:
    # ı ş ğ — a valid UTF-16-BOM'd file with Turkish characters must parse
    # identically to its UTF-8 twin, not just avoid raising.
    utf8 = '<SCAN comment="ışğ"></SCAN>'.encode()
    utf16 = b"\xff\xfe" + utf8.decode("utf-8").encode("utf-16-le")
    assert normalise_input(utf16) == utf8
    assert detect_bytes(utf16) == detect_bytes(utf8) == "qualys"


def test_bom_and_utf16_variants_of_a_real_fixture_detect_and_parse_the_same(
    tmp_path: Path,
) -> None:
    # Derived in-test from an existing fixture (no new fixture files committed):
    # BOM/UTF-16 must not change detection or the findings a real file yields.
    from grison.scanners import ImportOptions

    src = _FIX / "qualys" / "qualys_was_sample.xml"
    raw = src.read_bytes()
    assert detect_bytes(raw) == "qualys"

    cls = scanner_for("qualys")
    assert cls is not None
    baseline = cls().parse(normalise_input(raw), ImportOptions())
    assert baseline  # the sample fixture yields at least one finding

    variants = {
        "utf8-bom": b"\xef\xbb\xbf" + raw,
        "utf16-le-bom": b"\xff\xfe" + _utf16(raw, big_endian=False),
        "utf16-be-bom": b"\xfe\xff" + _utf16(raw, big_endian=True),
    }
    for label, variant in variants.items():
        assert detect_bytes(variant) == "qualys", label
        normalised = normalise_input(variant)
        findings = cls().parse(normalised, ImportOptions())
        assert findings == baseline, label


def test_utf16_bom_file_with_turkish_chars_parses_like_its_utf8_twin() -> None:
    # ı ş ğ inside real finding content (not just the sniffed root) — the full
    # detect + parse path must not mangle non-ASCII text when transcoding from
    # UTF-16, only when re-encoding it back to UTF-8 for the parser.
    from grison.scanners import ImportOptions

    utf8 = (
        '<?xml version="1.0"?>\n<SCAN><IP value="192.0.2.30"><VULNS>'
        '<CAT value="Vulnerabilities"><VULN number="86000">'
        "<TITLE>Işığın hızı: gizli açık ışğ bulundu</TITLE>"
        "<CONSEQUENCE>açıklama</CONSEQUENCE>"
        "<DIAGNOSIS>teşhis</DIAGNOSIS><SOLUTION>çözüm</SOLUTION>"
        "</VULN></CAT></VULNS></IP></SCAN>"
    ).encode()
    utf16 = b"\xff\xfe" + utf8.decode("utf-8").encode("utf-16-le")

    cls = scanner_for("qualys")
    assert cls is not None

    n8 = normalise_input(utf8)
    n16 = normalise_input(utf16)
    assert n8 == n16  # both normalise to the same UTF-8 bytes
    assert detect_bytes(n8) == detect_bytes(n16) == "qualys"

    findings8 = cls().parse(n8, ImportOptions())
    findings16 = cls().parse(n16, ImportOptions())
    assert findings8 == findings16
    assert findings8  # sanity: the fixture actually yielded a finding
    assert "Işığın hızı" in findings8[0].title  # Turkish chars intact, not mojibake


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
