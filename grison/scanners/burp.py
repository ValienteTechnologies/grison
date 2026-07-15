from __future__ import annotations

import re

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner


class BurpScanner(Scanner):
    name = "burp"
    label = "Burp Suite"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)
        issues = root.findall(".//issue")

        findings: list[Finding] = []
        grouped: dict[str, list[dict]] = {}  # type_id -> raw issue dicts

        for issue in issues:
            raw = {child.tag: (child.text or "").strip() for child in issue}
            host_el = issue.find("host")
            raw["_host_ip"] = host_el.get("ip", "") if host_el is not None else ""
            raw["_host_text"] = (host_el.text or "").strip() if host_el is not None else ""

            sev_raw = raw.get("severity", "information").lower()
            if sev_raw == "false positive":
                continue
            if sev_raw == "information":
                sev_raw = "informational"
            try:
                severity = Severity.from_str(sev_raw)
            except ValueError:
                severity = Severity.INFO

            if not self._severity_allowed(severity, opts):
                continue

            type_id = raw.get("type", "")
            if not self._plugin_allowed(type_id, opts):
                continue

            raw["_severity"] = severity
            grouped.setdefault(type_id, []).append(raw)

        for _type_id, group in grouped.items():
            finding = self._merge_group(group)
            if finding:
                findings.append(finding)

        return self.sort_by_severity(findings)

    def _merge_group(self, group: list[dict]) -> Finding | None:
        if not group:
            return None

        base = group[0]
        severity: Severity = base["_severity"]
        title = base.get("name", "Unknown")
        type_id = base.get("type", "")

        affected: list[str] = []
        for raw in group:
            host = raw.get("_host_text") or raw.get("_host_ip") or ""
            location = raw.get("location") or raw.get("path") or ""
            ip = raw.get("_host_ip", "")
            component = f"{host}{location}"
            if ip and ip != host:
                component += f" ({ip})"
            if component.strip():
                affected.append(component)

        refs_html = base.get("references", "")
        ref_urls = re.findall(r"<a\s+href=['\"]([^'\"]+)['\"]", refs_html, re.IGNORECASE)
        references = (
            "<ul>" + "".join(f"<li><a href=\"{u}\">{u}</a></li>" for u in ref_urls) + "</ul>"
            if ref_urls
            else ""
        )

        description = "\n\n".join(
            filter(None, [base.get("issueBackground", ""), base.get("issueDetail", "")])
        )
        mitigation = "\n\n".join(
            filter(None, [base.get("remediationBackground", ""), base.get("remediationDetail", "")])
        )

        return Finding(
            title=title,
            plugin_id=type_id,
            severity=severity,
            description=description,
            mitigation=mitigation,
            references=references,
            affected_components=list(dict.fromkeys(affected)),  # deduplicate, preserve order
        )
