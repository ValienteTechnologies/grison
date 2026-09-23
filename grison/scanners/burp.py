from __future__ import annotations

import re

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding
from grison.scanners.ir.severity import severity_or_info

from .base import AggregatedRecord, Aggregator, ImportOptions, RawOccurrence, Scanner, refs_to_html

_REF_ANCHOR = re.compile(r"<a\s+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)


class BurpScanner(Scanner):
    name = "burp"
    label = "Burp Suite"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        root = ET.fromstring(data)
        agg = Aggregator()

        for issue in root.findall(".//issue"):
            raw = {child.tag: (child.text or "").strip() for child in issue}
            host_el = issue.find("host")
            host_ip = host_el.get("ip", "") if host_el is not None else ""
            host_text = (host_el.text or "").strip() if host_el is not None else ""

            sev_raw = raw.get("severity", "information").lower()
            if sev_raw == "false positive":
                continue
            severity = severity_or_info(sev_raw)

            if not self._severity_allowed(severity, opts):
                continue

            type_id = raw.get("type", "")
            if not self._plugin_allowed(type_id, opts):
                continue

            host = host_text or host_ip
            location = raw.get("location") or raw.get("path") or ""
            component = f"{host}{location}"
            if host_ip and host_ip != host:
                component += f" ({host_ip})"

            description = "\n\n".join(
                filter(None, [raw.get("issueBackground", ""), raw.get("issueDetail", "")])
            )
            mitigation = "\n\n".join(
                filter(
                    None,
                    [raw.get("remediationBackground", ""), raw.get("remediationDetail", "")],
                )
            )
            # findall groups are (href, anchor_text); refs_to_html wants (anchor_text, href).
            ref_links: list[str | tuple[str, str]] = [
                (text, href) for href, text in _REF_ANCHOR.findall(raw.get("references", ""))
            ]

            agg.add(
                RawOccurrence(
                    key=type_id,
                    title=raw.get("name", "Unknown"),
                    severity=severity,
                    affected_component=component.strip(),
                    description=description,
                    mitigation=mitigation,
                    references=ref_links,
                )
            )

        findings = [self._to_finding(rec) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _to_finding(self, rec: AggregatedRecord) -> ScanFinding:
        return ScanFinding(
            title=rec.title,
            plugin_id=rec.key,
            severity=rec.severity,
            description=rec.description,
            mitigation=rec.mitigation,
            references=refs_to_html(rec.references),
            affected_components=rec.affected_components,
        )
