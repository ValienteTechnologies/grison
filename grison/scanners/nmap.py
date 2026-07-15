from __future__ import annotations

import re

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner


class NmapScanner(Scanner):
    name = "nmap"
    label = "Nmap"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        findings = self._parse_grepable(data) if opts.fmt == "grepable" else self._parse_xml(data)
        # Every finding here is INFO (open-port reporting has no other severity),
        # but --min-severity should still be able to suppress it like other scanners.
        return [f for f in findings if self._severity_allowed(f.severity, opts)]

    def _parse_xml(self, data: bytes) -> list[Finding]:
        root = ET.fromstring(data)
        findings: list[Finding] = []

        for host in root.findall(".//host"):
            if host.find("status[@state='up']") is None:
                # Some versions omit status when up; include if no status element
                status_el = host.find("status")
                if status_el is not None and status_el.get("state") != "up":
                    continue

            ip = ""
            hostname = ""
            for addr in host.findall("address"):
                if addr.get("addrtype") == "ipv4" or addr.get("addrtype") == "ipv6":
                    ip = addr.get("addr", "")
            for hn in host.findall(".//hostnames/hostname"):
                if hn.get("type") == "user" or not hostname:
                    hostname = hn.get("name", "")

            label = hostname or ip
            if not label:
                continue

            ports: list[dict] = []
            for port_el in host.findall(".//ports/port"):
                state_el = port_el.find("state")
                if state_el is None or state_el.get("state") != "open":
                    continue
                svc = port_el.find("service")
                ports.append(
                    {
                        "port": port_el.get("portid", ""),
                        "protocol": port_el.get("protocol", "tcp"),
                        "service": svc.get("name", "") if svc is not None else "",
                        "product": (
                            " ".join(
                                filter(None, [svc.get("product", ""), svc.get("version", "")])
                            )
                            if svc is not None
                            else ""
                        ),
                    }
                )

            if not ports:
                continue

            findings.append(self._build_finding(label, ip, hostname, ports))

        return findings

    def _parse_grepable(self, data: bytes) -> list[Finding]:
        findings: list[Finding] = []
        text = data.decode("utf-8", errors="replace")

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            host_match = re.search(r"Host:\s+(\S+)\s+\(([^)]*)\)", line)
            if not host_match:
                continue
            ip = host_match.group(1)
            hostname = host_match.group(2)

            ports_match = re.search(r"Ports:\s+(.+?)(?:\s+Ignored|$)", line)
            if not ports_match:
                continue

            ports: list[dict] = []
            for seg in ports_match.group(1).split(","):
                seg = seg.strip()
                # format: port/state/protocol/owner/service/...
                parts = seg.split("/")
                if len(parts) < 3:
                    continue
                state = parts[1].strip()
                if state != "open":
                    continue
                ports.append(
                    {
                        "port": parts[0].strip(),
                        "protocol": parts[2].strip(),
                        "service": parts[4].strip() if len(parts) > 4 else "",
                        "product": parts[6].strip() if len(parts) > 6 else "",
                    }
                )

            if ports:
                label = hostname or ip
                findings.append(self._build_finding(label, ip, hostname, ports))

        return findings

    def _build_finding(
        self, label: str, ip: str, hostname: str, ports: list[dict]
    ) -> Finding:
        rows = "".join(
            f"<tr><td>{p['port']}/{p['protocol']}</td>"
            f"<td>{p['service']}</td>"
            f"<td>{p['product']}</td></tr>"
            for p in ports
        )
        description = (
            f"<p>Open ports discovered on <strong>{label}</strong>"
            + (f" ({ip})" if hostname and ip else "")
            + f":</p>"
            f"<table><thead><tr><th>Port</th><th>Service</th><th>Product/Version</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
        components = [f"{p['port']}/{p['protocol']} ({p['service']})" for p in ports if p["port"]]

        return Finding(
            title=f"Open Ports – {label}",
            plugin_id=f"nmap-{ip or label}",
            severity=Severity.INFO,
            description=description,
            affected_components=components,
        )
