from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding, Severity
from grison.scanners.ir.cwe import normalize_cwe
from grison.scanners.ir.severity import severity_or_info

from .base import AggregatedRecord, Aggregator, ImportOptions, RawOccurrence, Scanner, refs_to_html

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
    return severity_or_info(raw)


class AcunetixScanner(Scanner):
    name = "acunetix"
    label = "Acunetix"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        root = ET.fromstring(data)

        scans = root.findall(".//Scan") if root.tag != "Scan" else [root]

        agg = Aggregator()

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

                cwe = normalize_cwe((item.findtext("CWE") or "").strip())

                tags = [
                    t.text.strip() for t in item.findall(".//Tags/Tag") if t.text and t.text.strip()
                ]

                refs: list[str | tuple[str, str]] = [
                    t for t in tags if t.upper().startswith("CVE-")
                ]
                for ref_el in item.findall(".//References/Reference"):
                    ref_text = (ref_el.text or "").strip()
                    if ref_text:
                        refs.append(ref_text)

                # Salvage patch: gw-import's parser dropped CVSS. Real Acunetix
                # exports carry a clean `CVSS:3.1/…` descriptor in <CVSS3><Descriptor>
                # (alongside legacy <CVSS> v2 and <CVSS4> blocks we ignore).
                cvss_vector = (item.findtext("CVSS3/Descriptor") or "").strip()

                agg.add(
                    RawOccurrence(
                        key=vuln_id,
                        title=(item.findtext("Name") or f"Finding {vuln_id}").strip(),
                        severity=severity,
                        affected_component=component,
                        cwe=cwe,
                        cvss_vector=cvss_vector,
                        description=(item.findtext("Description") or "").strip(),
                        impact=(item.findtext("Impact") or "").strip(),
                        mitigation=(item.findtext("Recommendation") or "").strip(),
                        references=refs,
                        tags=tags,
                    )
                )

        findings = [self._to_finding(rec) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _to_finding(self, rec: AggregatedRecord) -> ScanFinding:
        return ScanFinding(
            title=rec.title,
            plugin_id=rec.key,
            severity=rec.severity,
            cwe=rec.cwe,
            cvss_vector=rec.cvss_vector,
            description=rec.description,
            impact=rec.impact,
            mitigation=rec.mitigation,
            references=refs_to_html(rec.references),
            tags=rec.tags,
            affected_components=rec.affected_components,
        )
