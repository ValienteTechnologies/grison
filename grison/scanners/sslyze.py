from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner


@dataclass
class _VulnSpec:
    plugin_id: str
    title: str
    severity: Severity
    description: str
    mitigation: str


_PROTOCOL_VULNS: list[tuple[str, _VulnSpec]] = [
    (
        "ssl_2_0_cipher_suites",
        _VulnSpec(
            plugin_id="sslyze:ssl_2_0",
            title="SSL 2.0 Enabled",
            severity=Severity.CRITICAL,
            description=(
                "<p>SSL 2.0 is enabled on the target. SSL 2.0 is a severely flawed protocol "
                "vulnerable to multiple attacks including DROWN and POODLE. It has been "
                "formally deprecated since 2011 (RFC 6176).</p>"
            ),
            mitigation=(
                "<p>Disable SSL 2.0 in the server configuration. "
                "Configure a minimum TLS version of TLS 1.2.</p>"
            ),
        ),
    ),
    (
        "ssl_3_0_cipher_suites",
        _VulnSpec(
            plugin_id="sslyze:ssl_3_0",
            title="SSL 3.0 Enabled (POODLE)",
            severity=Severity.HIGH,
            description=(
                "<p>SSL 3.0 is enabled on the target. SSL 3.0 is vulnerable to the POODLE "
                "attack (CVE-2014-3566), which allows a man-in-the-middle attacker to "
                "recover plaintext from encrypted sessions.</p>"
            ),
            mitigation=(
                "<p>Disable SSL 3.0. Configure a minimum TLS version of TLS 1.2.</p>"
            ),
        ),
    ),
    (
        "tls_1_0_cipher_suites",
        _VulnSpec(
            plugin_id="sslyze:tls_1_0",
            title="TLS 1.0 Enabled",
            severity=Severity.MEDIUM,
            description=(
                "<p>TLS 1.0 is enabled on the target. TLS 1.0 is vulnerable to BEAST "
                "(CVE-2011-3389) and POODLE-over-TLS. It has been deprecated by RFC 8996 "
                "and is prohibited by PCI DSS since June 2018.</p>"
            ),
            mitigation=(
                "<p>Disable TLS 1.0 and TLS 1.1. Accept only TLS 1.2 and TLS 1.3.</p>"
            ),
        ),
    ),
    (
        "tls_1_1_cipher_suites",
        _VulnSpec(
            plugin_id="sslyze:tls_1_1",
            title="TLS 1.1 Enabled",
            severity=Severity.MEDIUM,
            description=(
                "<p>TLS 1.1 is enabled on the target. TLS 1.1 has been deprecated by "
                "RFC 8996. While fewer vulnerabilities exist than TLS 1.0, it is considered "
                "insecure by current standards.</p>"
            ),
            mitigation=(
                "<p>Disable TLS 1.1. Accept only TLS 1.2 and TLS 1.3.</p>"
            ),
        ),
    ),
]


class SslyzeScanner(Scanner):
    name = "sslyze"
    label = "SSLyze"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        doc = json.loads(data)
        server_results = doc.get("server_scan_results", [])

        aggregated: dict[str, tuple[_VulnSpec, list[str]]] = {}

        for server in server_results:
            if server.get("scan_status") == "ERROR":
                continue

            loc = server.get("server_location", {})
            host = loc.get("hostname") or loc.get("ip_address", "unknown")
            port = loc.get("port", 443)
            host_label = f"{host}:{port}"

            scan_result: dict[str, Any] = server.get("scan_result") or {}

            self._check_protocols(scan_result, host_label, aggregated, opts)
            self._check_heartbleed(scan_result, host_label, aggregated, opts)
            self._check_robot(scan_result, host_label, aggregated, opts)
            self._check_ccs(scan_result, host_label, aggregated, opts)
            self._check_compression(scan_result, host_label, aggregated, opts)
            self._check_certificates(scan_result, host_label, aggregated, opts)

        findings = [
            Finding(
                title=spec.title,
                plugin_id=plugin_id,
                severity=spec.severity,
                description=spec.description,
                mitigation=spec.mitigation,
                affected_components=list(dict.fromkeys(hosts)),
            )
            for plugin_id, (spec, hosts) in aggregated.items()
        ]
        return self.sort_by_severity(findings)

    def _add(
        self,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        spec: _VulnSpec,
        host: str,
        opts: ImportOptions,
    ) -> None:
        if not self._severity_allowed(spec.severity, opts):
            return
        if not self._plugin_allowed(spec.plugin_id, opts):
            return
        if spec.plugin_id not in aggregated:
            aggregated[spec.plugin_id] = (spec, [host])
        else:
            hosts = aggregated[spec.plugin_id][1]
            if host not in hosts:
                hosts.append(host)

    def _safe_result(self, scan_result: dict[str, Any], key: str) -> dict[str, Any] | None:
        entry = scan_result.get(key)
        if not entry:
            return None
        if entry.get("status") == "ERROR":
            return None
        return entry.get("result")

    def _check_protocols(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        for key, spec in _PROTOCOL_VULNS:
            result = self._safe_result(scan_result, key)
            if result is None:
                continue
            if result.get("accepted_cipher_suites"):
                self._add(aggregated, spec, host, opts)

    def _check_heartbleed(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        result = self._safe_result(scan_result, "heartbleed")
        if result and result.get("is_vulnerable_to_heartbleed"):
            self._add(
                aggregated,
                _VulnSpec(
                    plugin_id="sslyze:heartbleed",
                    title="Heartbleed (CVE-2014-0160)",
                    severity=Severity.CRITICAL,
                    description=(
                        "<p>The server is vulnerable to Heartbleed (CVE-2014-0160). "
                        "This critical OpenSSL vulnerability allows an attacker to read "
                        "up to 64 KB of server memory per request, potentially exposing "
                        "private keys, session tokens, and credentials.</p>"
                    ),
                    mitigation=(
                        "<p>Upgrade OpenSSL to a version that includes the fix for "
                        "CVE-2014-0160. Revoke and reissue all TLS certificates after "
                        "patching. Invalidate all active session tokens.</p>"
                    ),
                ),
                host,
                opts,
            )

    def _check_robot(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        result = self._safe_result(scan_result, "robot")
        if not result:
            return
        robot_enum = result.get("robot_attack_enum", "")
        if robot_enum.startswith("VULNERABLE"):
            self._add(
                aggregated,
                _VulnSpec(
                    plugin_id="sslyze:robot",
                    title="ROBOT Attack",
                    severity=Severity.HIGH,
                    description=(
                        "<p>The server is vulnerable to the ROBOT attack (Return Of "
                        "Bleichenbacher's Oracle Threat). This allows decryption of "
                        "RSA-encrypted TLS sessions and forging of RSA signatures. "
                        f"Enum value: <code>{robot_enum}</code></p>"
                    ),
                    mitigation=(
                        "<p>Disable RSA key exchange cipher suites. Use forward-secret "
                        "cipher suites (ECDHE/DHE) exclusively.</p>"
                    ),
                ),
                host,
                opts,
            )

    def _check_ccs(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        result = self._safe_result(scan_result, "openssl_ccs_injection")
        if result and result.get("is_vulnerable_to_ccs_injection"):
            self._add(
                aggregated,
                _VulnSpec(
                    plugin_id="sslyze:openssl_ccs",
                    title="OpenSSL CCS Injection (CVE-2014-0224)",
                    severity=Severity.HIGH,
                    description=(
                        "<p>The server is vulnerable to the OpenSSL CCS Injection attack "
                        "(CVE-2014-0224). An attacker performing a man-in-the-middle attack "
                        "can force the use of weak keying material, potentially allowing "
                        "decryption or modification of traffic.</p>"
                    ),
                    mitigation=(
                        "<p>Upgrade OpenSSL to a patched version. "
                        "Apply vendor security patches immediately.</p>"
                    ),
                ),
                host,
                opts,
            )

    def _check_compression(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        result = self._safe_result(scan_result, "tls_compression")
        if result and result.get("supports_compression"):
            self._add(
                aggregated,
                _VulnSpec(
                    plugin_id="sslyze:tls_compression",
                    title="TLS Compression Enabled (CRIME)",
                    severity=Severity.MEDIUM,
                    description=(
                        "<p>TLS compression is enabled on the server. This makes the server "
                        "vulnerable to the CRIME attack (CVE-2012-4929), which can allow "
                        "an attacker to recover sensitive data such as session tokens from "
                        "compressed TLS streams.</p>"
                    ),
                    mitigation=(
                        "<p>Disable TLS compression in the server configuration. "
                        "In OpenSSL, set <code>SSL_OP_NO_COMPRESSION</code>. "
                        "In nginx, set <code>gzip off</code> in the SSL context.</p>"
                    ),
                ),
                host,
                opts,
            )

    def _check_certificates(
        self,
        scan_result: dict[str, Any],
        host: str,
        aggregated: dict[str, tuple[_VulnSpec, list[str]]],
        opts: ImportOptions,
    ) -> None:
        result = self._safe_result(scan_result, "certificate_info")
        if not result:
            return

        for deployment in result.get("certificate_deployments", []):
            if not deployment.get("leaf_certificate_is_within_validity_period", True):
                self._add(
                    aggregated,
                    _VulnSpec(
                        plugin_id="sslyze:cert_expired",
                        title="Expired TLS Certificate",
                        severity=Severity.HIGH,
                        description=(
                            "<p>The server presents an expired TLS certificate. "
                            "Clients may reject the connection or display security warnings. "
                            "Expired certificates indicate a lapse in certificate lifecycle "
                            "management.</p>"
                        ),
                        mitigation=(
                            "<p>Renew the TLS certificate immediately. "
                            "Implement automated certificate renewal (e.g., Let's Encrypt "
                            "with Certbot or the ACME protocol).</p>"
                        ),
                    ),
                    host,
                    opts,
                )

            if not deployment.get("leaf_certificate_subject_matches_hostname", True):
                self._add(
                    aggregated,
                    _VulnSpec(
                        plugin_id="sslyze:cert_hostname_mismatch",
                        title="TLS Certificate Hostname Mismatch",
                        severity=Severity.MEDIUM,
                        description=(
                            "<p>The TLS certificate Subject/SAN does not match the hostname "
                            "being connected to. This causes certificate validation errors in "
                            "strict clients and may indicate misconfiguration or use of the "
                            "wrong certificate.</p>"
                        ),
                        mitigation=(
                            "<p>Replace the certificate with one that includes the correct "
                            "hostname in the Subject Alternative Names (SAN) field.</p>"
                        ),
                    ),
                    host,
                    opts,
                )

            path_val_errs = deployment.get("path_validation_results", [])
            if any(not pv.get("verified_certificate_chain") for pv in path_val_errs):
                self._add(
                    aggregated,
                    _VulnSpec(
                        plugin_id="sslyze:cert_untrusted",
                        title="Untrusted or Self-Signed TLS Certificate",
                        severity=Severity.MEDIUM,
                        description=(
                            "<p>The TLS certificate is not trusted by one or more evaluated "
                            "trust stores. This may indicate a self-signed certificate, an "
                            "internal CA not in the system trust store, or a broken "
                            "certificate chain.</p>"
                        ),
                        mitigation=(
                            "<p>Replace self-signed certificates with ones signed by a trusted "
                            "public CA. Ensure the full certificate chain (including "
                            "intermediates) is served by the server.</p>"
                        ),
                    ),
                    host,
                    opts,
                )

            received_chain = deployment.get("received_chain") or []
            public_key = received_chain[0].get("public_key", {}) if received_chain else {}
            pk_type = public_key.get("algorithm", "")
            pk_size = public_key.get("key_size", 9999)
            weak = (pk_type == "RSA" and pk_size < 2048) or (
                pk_type == "EC" and pk_size < 224
            )
            if weak:
                self._add(
                    aggregated,
                    _VulnSpec(
                        plugin_id="sslyze:cert_weak_key",
                        title="Weak TLS Certificate Public Key",
                        severity=Severity.MEDIUM,
                        description=(
                            f"<p>The TLS certificate uses a weak public key "
                            f"(<code>{pk_type} {pk_size}-bit</code>). "
                            "Keys shorter than RSA-2048 or EC-224 are considered insufficient "
                            "for long-term security.</p>"
                        ),
                        mitigation=(
                            "<p>Replace the certificate with one using at least RSA-2048 "
                            "or EC-256 (P-256 or stronger).</p>"
                        ),
                    ),
                    host,
                    opts,
                )
