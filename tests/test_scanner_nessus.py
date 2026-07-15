"""Nessus parser test (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from grison.scanners import ImportOptions, NessusScanner
from grison.scanners.ir import Severity
from grison.scanners.nessus import _cvss2_to_cvss3

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_findings() -> None:
    findings = NessusScanner().parse(load("nessus_sample.xml"), ImportOptions())
    assert len(findings) == 1
    assert findings[0].title == "Outdated TLS Version"
    assert findings[0].severity == Severity.MEDIUM
    assert findings[0].affected_components == ["host.example.com:443/tcp (https)"]


def test_cvss2_to_cvss3_preserves_av_with_hash_prefix() -> None:
    # Real Nessus vectors are prefixed "CVSS2#...": AV must not be dropped.
    v3 = _cvss2_to_cvss3("CVSS2#AV:L/AC:L/Au:N/C:P/I:P/A:P")
    assert "/AV:L/" in v3


def test_cvss2_to_cvss3_preserves_av_bare() -> None:
    v3 = _cvss2_to_cvss3("AV:A/AC:L/Au:N/C:P/I:P/A:P")
    assert "/AV:A/" in v3


def test_cvss2_to_cvss3_preserves_av_parenthesized() -> None:
    v3 = _cvss2_to_cvss3("(AV:N/AC:L/Au:N/C:P/I:P/A:P)")
    assert "/AV:N/" in v3


def test_cvss2_to_cvss3_full_conversion() -> None:
    v3 = _cvss2_to_cvss3("CVSS2#AV:N/AC:L/Au:N/C:P/I:P/A:P")
    assert v3 == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L"
