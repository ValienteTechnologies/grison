from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner

_NUMERIC_SEV: dict[str, Severity] = {
    "0": Severity.INFO,
    "1": Severity.LOW,
    "2": Severity.MEDIUM,
    "3": Severity.HIGH,
    "4": Severity.CRITICAL,
}


def _parse_severity(raw: str) -> Severity:
    raw = raw.strip().lower()
    if raw in _NUMERIC_SEV:
        return _NUMERIC_SEV[raw]
    try:
        return Severity.from_str(raw)
    except ValueError:
        return Severity.INFO


class AcunetixScanner(Scanner):
    name = "acunetix"
    label = "Acunetix"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)

        scans = root.findall(".//Scan") if root.tag != "Scan" else [root]

        aggregated: dict[str, dict] = {}

        for scan in scans:
            start_url = (scan.findtext("StartURL") or "").strip()

            for item in scan.findall(".//ReportItem"):
                vuln_id = (item.findtext("VulnID") or "").strip()
                if not vuln_id:
                    vuln_id = (item.findtext("Name") or "unknown").strip()

                if not self._plugin_allowed(vuln_id, opts):
                    continue

                sev_raw = (item.findtext("Severity") or "informational").strip()
                severity = _parse_severity(sev_raw)

                if not self._severity_allowed(severity, opts):
                    continue

                affected_item = (item.findtext("AffectedItem") or "").strip()
                component = f"{start_url}{affected_item}" if affected_item else start_url

                if vuln_id not in aggregated:
                    cwe_raw = (item.findtext("CWE") or "").strip()
                    if cwe_raw.upper().startswith("CWE-"):
                        cwe_raw = cwe_raw[4:]
                    cwe_str = f"CWE-{cwe_raw}" if cwe_raw.isdigit() else ""

                    tags = [
                        t.text.strip()
                        for t in item.findall(".//Tags/Tag")
                        if t.text and t.text.strip()
                    ]

                    refs: list[str] = []
                    for tag in tags:
                        if tag.upper().startswith("CVE-"):
                            refs.append(tag)
                    for ref_el in item.findall(".//References/Reference"):
                        ref_text = (ref_el.text or "").strip()
                        if ref_text:
                            refs.append(ref_text)

                    if refs:
                        refs_html = "<ul>" + "".join(
                            f'<li><a href="{r}">{r}</a></li>'
                            if r.startswith("http")
                            else f"<li>{r}</li>"
                            for r in refs
                        ) + "</ul>"
                    else:
                        refs_html = ""

                    # Salvage patch: gw-import's parser dropped CVSS. Real Acunetix
                    # exports carry a clean `CVSS:3.1/…` descriptor in <CVSS3><Descriptor>
                    # (alongside legacy <CVSS> v2 and <CVSS4> blocks we ignore).
                    cvss_vector = (item.findtext("CVSS3/Descriptor") or "").strip()

                    aggregated[vuln_id] = {
                        "title": (item.findtext("Name") or f"Finding {vuln_id}").strip(),
                        "severity": severity,
                        "cwe": cwe_str,
                        "cvss_vector": cvss_vector,
                        "description": (item.findtext("Description") or "").strip(),
                        "impact": (item.findtext("Impact") or "").strip(),
                        "mitigation": (item.findtext("Recommendation") or "").strip(),
                        "references": refs_html,
                        "tags": tags,
                        "affected": [component] if component else [],
                    }
                else:
                    if component and component not in aggregated[vuln_id]["affected"]:
                        aggregated[vuln_id]["affected"].append(component)

        findings: list[Finding] = [
            Finding(
                title=meta["title"],
                plugin_id=vuln_id,
                severity=meta["severity"],
                cwe=meta["cwe"],
                cvss_vector=meta["cvss_vector"],
                description=meta["description"],
                impact=meta["impact"],
                mitigation=meta["mitigation"],
                references=meta["references"],
                tags=meta["tags"],
                affected_components=meta["affected"],
            )
            for vuln_id, meta in aggregated.items()
        ]

        return self.sort_by_severity(findings)
