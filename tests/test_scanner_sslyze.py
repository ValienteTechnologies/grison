"""SSLyze parser tests (ported from gw-import)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grison.scanners import ImportOptions, SslyzeScanner
from grison.scanners.ir import Severity

FIXTURES = Path(__file__).parent / "fixtures" / "scanners"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def scanner() -> SslyzeScanner:
    return SslyzeScanner()


@pytest.fixture
def opts() -> ImportOptions:
    return ImportOptions()


def test_scanner_metadata() -> None:
    assert SslyzeScanner.name == "sslyze"
    assert SslyzeScanner.label == "SSLyze"


def test_ssl2_detected(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    f = next((f for f in findings if f.plugin_id == "sslyze:ssl_2_0"), None)
    assert f is not None
    assert f.severity == Severity.CRITICAL


def test_tls10_detected(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    f = next((f for f in findings if f.plugin_id == "sslyze:tls_1_0"), None)
    assert f is not None
    assert f.severity == Severity.MEDIUM


def test_heartbleed_detected(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    f = next((f for f in findings if f.plugin_id == "sslyze:heartbleed"), None)
    assert f is not None
    assert f.severity == Severity.CRITICAL


def test_cert_expired_detected(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    f = next((f for f in findings if f.plugin_id == "sslyze:cert_expired"), None)
    assert f is not None
    assert f.severity == Severity.HIGH


def test_cert_untrusted_detected(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    f = next((f for f in findings if f.plugin_id == "sslyze:cert_untrusted"), None)
    assert f is not None


def test_clean_server_no_findings(scanner: SslyzeScanner) -> None:
    doc = {
        "server_scan_results": [
            {
                "scan_status": "COMPLETED",
                "server_location": {"hostname": "clean.example.com", "port": 443},
                "scan_result": {
                    "ssl_2_0_cipher_suites": {
                        "status": "COMPLETED",
                        "result": {"accepted_cipher_suites": []},
                    },
                    "tls_1_0_cipher_suites": {
                        "status": "COMPLETED",
                        "result": {"accepted_cipher_suites": []},
                    },
                    "heartbleed": {
                        "status": "COMPLETED",
                        "result": {"is_vulnerable_to_heartbleed": False},
                    },
                    "robot": {
                        "status": "COMPLETED",
                        "result": {"robot_attack_enum": "NOT_VULNERABLE_NO_ORACLE"},
                    },
                    "openssl_ccs_injection": {
                        "status": "COMPLETED",
                        "result": {"is_vulnerable_to_ccs_injection": False},
                    },
                    "tls_compression": {
                        "status": "COMPLETED",
                        "result": {"supports_compression": False},
                    },
                    "certificate_info": {
                        "status": "COMPLETED",
                        "result": {"certificate_deployments": []},
                    },
                },
            }
        ]
    }
    findings = scanner.parse(json.dumps(doc).encode(), ImportOptions())
    assert findings == []


def test_errored_server_skipped(scanner: SslyzeScanner) -> None:
    doc = {
        "server_scan_results": [
            {
                "scan_status": "ERROR",
                "server_location": {"hostname": "bad.example.com", "port": 443},
                "scan_result": None,
            }
        ]
    }
    findings = scanner.parse(json.dumps(doc).encode(), ImportOptions())
    assert findings == []


def test_severity_filter(scanner: SslyzeScanner) -> None:
    findings = scanner.parse(
        load("sslyze_sample.json"),
        ImportOptions(severity_filter={Severity.CRITICAL}),
    )
    assert all(f.severity == Severity.CRITICAL for f in findings)


def test_affected_components_aggregated(scanner: SslyzeScanner) -> None:
    doc = {
        "server_scan_results": [
            {
                "server_location": {"hostname": "host1.example.com", "port": 443},
                "scan_result": {
                    "tls_1_0_cipher_suites": {
                        "status": "COMPLETED",
                        "result": {
                            "accepted_cipher_suites": [
                                {"cipher_suite": {"name": "TLS_RSA_WITH_AES_128_CBC_SHA"}}
                            ]
                        },
                    }
                },
            },
            {
                "server_location": {"hostname": "host2.example.com", "port": 443},
                "scan_result": {
                    "tls_1_0_cipher_suites": {
                        "status": "COMPLETED",
                        "result": {
                            "accepted_cipher_suites": [
                                {"cipher_suite": {"name": "TLS_RSA_WITH_AES_128_CBC_SHA"}}
                            ]
                        },
                    }
                },
            },
        ]
    }
    findings = scanner.parse(json.dumps(doc).encode(), ImportOptions())
    f = next(f for f in findings if f.plugin_id == "sslyze:tls_1_0")
    assert len(f.affected_components) == 2
    assert "host1.example.com:443" in f.affected_components
    assert "host2.example.com:443" in f.affected_components


def test_findings_sorted_by_severity(scanner: SslyzeScanner, opts: ImportOptions) -> None:
    findings = scanner.parse(load("sslyze_sample.json"), opts)
    if len(findings) > 1:
        sev_order = list(Severity)
        indices = [sev_order.index(f.severity) for f in findings]
        assert indices == sorted(indices, reverse=True)


def test_empty_input(scanner: SslyzeScanner) -> None:
    findings = scanner.parse(json.dumps({"server_scan_results": []}).encode(), ImportOptions())
    assert findings == []


def test_plugin_exclude(scanner: SslyzeScanner) -> None:
    findings = scanner.parse(
        load("sslyze_sample.json"),
        ImportOptions(exclude_plugins=["sslyze:ssl_2_0"]),
    )
    assert all(f.plugin_id != "sslyze:ssl_2_0" for f in findings)


def test_robot_not_triggered_when_not_vulnerable(scanner: SslyzeScanner) -> None:
    doc = {
        "server_scan_results": [
            {
                "server_location": {"hostname": "host.example.com", "port": 443},
                "scan_result": {
                    "robot": {
                        "status": "COMPLETED",
                        "result": {"robot_attack_enum": "NOT_VULNERABLE_NO_ORACLE"},
                    }
                },
            }
        ]
    }
    findings = scanner.parse(json.dumps(doc).encode(), ImportOptions())
    assert not any(f.plugin_id == "sslyze:robot" for f in findings)
