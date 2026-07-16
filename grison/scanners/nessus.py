from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity
from grison.scanners.ir.cvss2 import cvss2_to_cvss3 as _cvss2_to_cvss3

from .base import ImportOptions, Scanner


class NessusScanner(Scanner):
    name = "nessus"
    label = "Nessus"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)
        # Aggregate: plugin_id -> {meta, affected_components, refs}
        aggregated: dict[str, dict] = {}

        for host in root.findall(".//ReportHost"):
            hostname = host.get("name", "")

            for item in host.findall("ReportItem"):
                plugin_id = item.get("pluginID", "")
                svc = item.get("svc_name", "")
                port = item.get("port", "")
                protocol = item.get("protocol", "tcp")

                if opts.no_snoozed and (item.findtext("snoozed") or "").strip():
                    continue

                if not self._plugin_allowed(plugin_id, opts):
                    continue

                risk_raw = (item.findtext("risk_factor") or "none").strip().lower()
                if risk_raw == "none":
                    risk_raw = "informational"
                try:
                    severity = Severity.from_str(risk_raw)
                except ValueError:
                    severity = Severity.INFO

                if not self._severity_allowed(severity, opts):
                    continue

                component = f"{hostname}:{port}/{protocol}"
                if svc:
                    component += f" ({svc})"

                if plugin_id not in aggregated:
                    # Prefer cvss3_vector by provenance, not by prefix-sniffing the
                    # (possibly v2) cvss_vector field: real-world exports sometimes
                    # populate both, and cvss3_vector sometimes lacks its "CVSS:3.x/"
                    # prefix — either way it's already v3 and must never be routed
                    # through the v2 converter, which would silently zero its impact.
                    cvss3_raw = (item.findtext("cvss3_vector") or "").strip()
                    if cvss3_raw:
                        cvss_raw = (
                            cvss3_raw
                            if cvss3_raw.startswith("CVSS:3")
                            else f"CVSS:3.0/{cvss3_raw}"
                        )
                    else:
                        cvss2_raw = (item.findtext("cvss_vector") or "").strip()
                        cvss_raw = _cvss2_to_cvss3(cvss2_raw) if cvss2_raw else ""

                    cwe_raw = (item.findtext("cwe") or "").strip()
                    cwe = f"CWE-{cwe_raw}" if cwe_raw.isdigit() else ""

                    see_also_raw = (item.findtext("see_also") or "").strip()
                    refs = [u.strip() for u in see_also_raw.splitlines() if u.strip()]

                    aggregated[plugin_id] = {
                        "title": item.get("pluginName", f"Plugin {plugin_id}"),
                        "severity": severity,
                        "cvss_vector": cvss_raw,
                        "cwe": cwe,
                        "description": (item.findtext("description") or "").strip(),
                        "mitigation": (item.findtext("solution") or "").strip(),
                        "synopsis": (item.findtext("synopsis") or "").strip(),
                        "refs": refs,
                        "affected": [component],
                    }
                else:
                    if component not in aggregated[plugin_id]["affected"]:
                        aggregated[plugin_id]["affected"].append(component)

        findings: list[Finding] = []
        for plugin_id, meta in aggregated.items():
            refs_html = (
                "<ul>"
                + "".join(f'<li><a href="{r}">{r}</a></li>' for r in meta["refs"])
                + "</ul>"
                if meta["refs"]
                else ""
            )
            description = (
                meta["synopsis"] + "\n\n" + meta["description"]
                if meta["synopsis"]
                else meta["description"]
            )
            findings.append(
                Finding(
                    title=meta["title"],
                    plugin_id=plugin_id,
                    severity=meta["severity"],
                    cvss_vector=meta["cvss_vector"],
                    cwe=meta["cwe"],
                    description=description.strip(),
                    mitigation=meta["mitigation"],
                    references=refs_html,
                    affected_components=meta["affected"],
                )
            )

        return self.sort_by_severity(findings)
