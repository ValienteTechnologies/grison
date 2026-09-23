from __future__ import annotations

from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding, Severity
from grison.scanners.ir.cvss2 import ensure_cvss3_prefix
from grison.scanners.ir.severity import severity_or_info

from .base import AggregatedRecord, Aggregator, ImportOptions, RawOccurrence, Scanner, refs_to_html

_SEVERITY_MAP = {
    "1": Severity.INFO,
    "2": Severity.LOW,
    "3": Severity.MEDIUM,
    "4": Severity.HIGH,
    "5": Severity.CRITICAL,
}


def _parse_severity(raw: str) -> Severity:
    mapped = _SEVERITY_MAP.get(raw)
    if mapped is not None:
        return mapped
    # Qualys severities are numeric in practice; this only fires for an
    # unrecognised code.
    return severity_or_info(raw)


class QualysScanner(Scanner):
    name = "qualys"
    label = "Qualys"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        root = ET.fromstring(data)
        tag = root.tag

        if tag == "WAS_SCAN_REPORT":
            return self._parse_was(root, opts)
        elif tag == "SCAN":
            return self._parse_vuln(root, opts)
        else:
            raise ValueError(
                f"Unrecognised Qualys root element: {tag!r}. Expected WAS_SCAN_REPORT or SCAN."
            )

    def _parse_was(self, root: Element, opts: ImportOptions) -> list[ScanFinding]:
        # Build glossary: QID -> {title, severity, description, solution, ...}
        glossary: dict[str, dict] = {}
        for qid_el in root.findall(".//GLOSSARY/QID_LIST/QID"):
            qid = qid_el.findtext("QID") or ""
            if qid:
                # WAS glossary entries carry a ready CVSS3.x vector under
                # CVSS_V3/VECTOR_STRING. Qualys emits it bare (no "CVSS:3.x/"
                # prefix) — prepend one, same as the Nessus cvss3_vector handling.
                cvss3_raw = (qid_el.findtext("CVSS_V3/VECTOR_STRING") or "").strip()
                cvss_vector = ensure_cvss3_prefix(cvss3_raw) if cvss3_raw else ""

                glossary[qid] = {
                    "title": qid_el.findtext("TITLE") or f"QID {qid}",
                    "severity": qid_el.findtext("SEVERITY") or "3",
                    "description": qid_el.findtext("DESCRIPTION") or "",
                    "impact": qid_el.findtext("IMPACT") or "",
                    "solution": qid_el.findtext("SOLUTION") or "",
                    "cvss_vector": cvss_vector,
                    "cve_list": [c.text or "" for c in qid_el.findall(".//CVE_LIST/CVE/ID")],
                }

        agg = Aggregator()
        for vuln in root.findall(".//RESULTS/VULNERABILITY_LIST/VULNERABILITY"):
            qid = vuln.findtext("QID") or ""
            url = vuln.findtext("URL") or ""
            meta = glossary.get(qid, {"title": f"QID {qid}", "severity": "3"})

            severity = _parse_severity(str(meta.get("severity", "3")))
            if not self._severity_allowed(severity, opts):
                continue
            if not self._plugin_allowed(qid, opts):
                continue

            agg.add(
                RawOccurrence(
                    key=qid,
                    title=meta.get("title", f"QID {qid}"),
                    severity=severity,
                    affected_component=url,
                    cvss_vector=meta.get("cvss_vector", ""),
                    description=meta.get("description", ""),
                    impact=meta.get("impact", ""),
                    mitigation=meta.get("solution", ""),
                    references=list(meta.get("cve_list", [])),
                )
            )

        return self._finish(agg)

    def _parse_vuln(self, root: Element, opts: ImportOptions) -> list[ScanFinding]:
        agg = Aggregator()

        for ip_el in root.findall(".//IP"):
            target = ip_el.get("value", ip_el.get("addr", ""))
            for cat in ip_el.findall(".//VULNS/CAT"):
                for vuln in cat.findall("VULN"):
                    qid = vuln.get("number", "")
                    severity = _parse_severity(vuln.get("severity", "3"))
                    if not self._severity_allowed(severity, opts):
                        continue
                    if not self._plugin_allowed(qid, opts):
                        continue

                    title = vuln.findtext("TITLE") or f"QID {qid}"
                    cve_list = [c.text or "" for c in vuln.findall(".//CVE_ID_LIST/CVE_ID")]

                    agg.add(
                        RawOccurrence(
                            key=qid,
                            title=title,
                            severity=severity,
                            affected_component=target,
                            description=vuln.findtext("CONSEQUENCE") or "",
                            impact=vuln.findtext("DIAGNOSIS") or "",
                            mitigation=vuln.findtext("SOLUTION") or "",
                            references=list(cve_list),
                        )
                    )

        return self._finish(agg)

    def _finish(self, agg: Aggregator) -> list[ScanFinding]:
        findings = [self._to_finding(rec) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _to_finding(self, rec: AggregatedRecord) -> ScanFinding:
        return ScanFinding(
            title=rec.title,
            plugin_id=rec.key,
            severity=rec.severity,
            cvss_vector=rec.cvss_vector,
            description=rec.description,
            impact=rec.impact,
            mitigation=rec.mitigation,
            references=refs_to_html(rec.references),
            affected_components=rec.affected_components,
        )
