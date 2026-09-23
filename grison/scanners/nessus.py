from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding
from grison.scanners.ir.cvss2 import CvssConversionError, ensure_cvss3_prefix
from grison.scanners.ir.cvss2 import cvss2_to_cvss3 as _cvss2_to_cvss3
from grison.scanners.ir.cwe import normalize_cwe
from grison.scanners.ir.severity import severity_or_info

from .base import AggregatedRecord, Aggregator, ImportOptions, RawOccurrence, Scanner, refs_to_html


class NessusScanner(Scanner):
    name = "nessus"
    label = "Nessus"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        root = ET.fromstring(data)
        agg = Aggregator()

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
                severity = severity_or_info(risk_raw)

                if not self._severity_allowed(severity, opts):
                    continue

                component = f"{hostname}:{port}/{protocol}"
                if svc:
                    component += f" ({svc})"

                # Prefer cvss3_vector by provenance, not by prefix-sniffing the
                # (possibly v2) cvss_vector field: real-world exports sometimes
                # populate both, and cvss3_vector sometimes lacks its "CVSS:3.x/"
                # prefix — either way it's already v3 and must never be routed
                # through the v2 converter, which would silently zero its impact.
                cvss3_raw = (item.findtext("cvss3_vector") or "").strip()
                notes: list[str] = []
                if cvss3_raw:
                    cvss_raw = ensure_cvss3_prefix(cvss3_raw)
                else:
                    cvss2_raw = (item.findtext("cvss_vector") or "").strip()
                    if cvss2_raw:
                        try:
                            cvss_raw = _cvss2_to_cvss3(cvss2_raw)
                        except CvssConversionError:
                            cvss_raw = ""
                            notes.append(
                                f"CVSS v2 vector {cvss2_raw} could not be converted, dropped"
                            )
                    else:
                        cvss_raw = ""

                cwe = normalize_cwe((item.findtext("cwe") or "").strip())

                see_also_raw = (item.findtext("see_also") or "").strip()
                refs: list[str | tuple[str, str]] = [
                    u.strip() for u in see_also_raw.splitlines() if u.strip()
                ]

                synopsis = (item.findtext("synopsis") or "").strip()
                raw_description = (item.findtext("description") or "").strip()
                description = f"{synopsis}\n\n{raw_description}" if synopsis else raw_description

                agg.add(
                    RawOccurrence(
                        key=plugin_id,
                        title=item.get("pluginName", f"Plugin {plugin_id}"),
                        severity=severity,
                        affected_component=component,
                        cvss_vector=cvss_raw,
                        cwe=cwe,
                        description=description.strip(),
                        mitigation=(item.findtext("solution") or "").strip(),
                        references=refs,
                        notes=notes,
                    )
                )

        findings = [self._to_finding(rec) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _to_finding(self, rec: AggregatedRecord) -> ScanFinding:
        return ScanFinding(
            title=rec.title,
            plugin_id=rec.key,
            severity=rec.severity,
            cvss_vector=rec.cvss_vector,
            cwe=rec.cwe,
            description=rec.description,
            mitigation=rec.mitigation,
            references=refs_to_html(rec.references),
            affected_components=rec.affected_components,
            notes=rec.notes,
        )
