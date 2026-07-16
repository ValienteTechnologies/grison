from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner

_SEVERITY_MAP = {
    "1": Severity.INFO,
    "2": Severity.LOW,
    "3": Severity.MEDIUM,
    "4": Severity.HIGH,
    "5": Severity.CRITICAL,
}


class QualysScanner(Scanner):
    name = "qualys"
    label = "Qualys"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)
        tag = root.tag

        if tag == "WAS_SCAN_REPORT":
            return self._parse_was(root, opts)
        elif tag == "SCAN":
            return self._parse_vuln(root, opts)
        else:
            raise ValueError(
                f"Unrecognised Qualys root element: {tag!r}. "
                "Expected WAS_SCAN_REPORT or SCAN."
            )

    def _parse_was(self, root: ET.Element, opts: ImportOptions) -> list[Finding]:
        # Build glossary: QID -> {title, severity, description, solution, ...}
        glossary: dict[str, dict] = {}
        for qid_el in root.findall(".//GLOSSARY/QID_LIST/QID"):
            qid = qid_el.findtext("QID") or ""
            if qid:
                # WAS glossary entries carry a ready CVSS3.x vector under
                # CVSS_V3/VECTOR_STRING. Qualys emits it bare (no "CVSS:3.x/"
                # prefix) — prepend one, same as the Nessus cvss3_vector handling.
                cvss3_raw = (qid_el.findtext("CVSS_V3/VECTOR_STRING") or "").strip()
                if cvss3_raw:
                    cvss_vector = (
                        cvss3_raw if cvss3_raw.startswith("CVSS:3") else f"CVSS:3.0/{cvss3_raw}"
                    )
                else:
                    cvss_vector = ""

                glossary[qid] = {
                    "title": qid_el.findtext("TITLE") or f"QID {qid}",
                    "severity": qid_el.findtext("SEVERITY") or "3",
                    "description": qid_el.findtext("DESCRIPTION") or "",
                    "impact": qid_el.findtext("IMPACT") or "",
                    "solution": qid_el.findtext("SOLUTION") or "",
                    "cvss_vector": cvss_vector,
                    "cve_list": [
                        c.text or ""
                        for c in qid_el.findall(".//CVE_LIST/CVE/ID")
                    ],
                }

        # Aggregate vulnerabilities by QID
        aggregated: dict[str, dict] = {}
        for vuln in root.findall(".//RESULTS/VULNERABILITY_LIST/VULNERABILITY"):
            qid = vuln.findtext("QID") or ""
            url = vuln.findtext("URL") or ""
            meta = glossary.get(qid, {"title": f"QID {qid}", "severity": "3"})
            if qid not in aggregated:
                aggregated[qid] = {**meta, "affected": [url] if url else []}
            else:
                if url and url not in aggregated[qid]["affected"]:
                    aggregated[qid]["affected"].append(url)

        return self._build_findings(aggregated, opts)

    def _parse_vuln(self, root: ET.Element, opts: ImportOptions) -> list[Finding]:
        aggregated: dict[str, dict] = {}

        for ip_el in root.findall(".//IP"):
            target = ip_el.get("value", ip_el.get("addr", ""))
            for cat in ip_el.findall(".//VULNS/CAT"):
                for vuln in cat.findall("VULN"):
                    qid = vuln.get("number", "")
                    sev = vuln.get("severity", "3")
                    title = vuln.findtext("TITLE") or f"QID {qid}"
                    if qid not in aggregated:
                        aggregated[qid] = {
                            "title": title,
                            "severity": sev,
                            "description": vuln.findtext("CONSEQUENCE") or "",
                            "impact": vuln.findtext("DIAGNOSIS") or "",
                            "solution": vuln.findtext("SOLUTION") or "",
                            "cve_list": [
                                c.text or "" for c in vuln.findall(".//CVE_ID_LIST/CVE_ID")
                            ],
                            "affected": [target] if target else [],
                        }
                    else:
                        if target and target not in aggregated[qid]["affected"]:
                            aggregated[qid]["affected"].append(target)

        return self._build_findings(aggregated, opts)

    def _build_findings(self, aggregated: dict[str, dict], opts: ImportOptions) -> list[Finding]:
        findings: list[Finding] = []
        for qid, meta in aggregated.items():
            severity = _SEVERITY_MAP.get(str(meta.get("severity", "3")), Severity.MEDIUM)
            if not self._severity_allowed(severity, opts):
                continue
            if not self._plugin_allowed(qid, opts):
                continue

            cves = meta.get("cve_list", [])
            refs_html = (
                "<ul>"
                + "".join(f"<li>{c}</li>" for c in cves if c)
                + "</ul>"
                if cves
                else ""
            )

            findings.append(
                Finding(
                    title=meta.get("title", f"QID {qid}"),
                    plugin_id=qid,
                    severity=severity,
                    cvss_vector=meta.get("cvss_vector", ""),
                    description=meta.get("description", ""),
                    impact=meta.get("impact", ""),
                    mitigation=meta.get("solution", ""),
                    references=refs_html,
                    affected_components=meta.get("affected", []),
                )
            )

        return self.sort_by_severity(findings)
