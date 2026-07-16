from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, cvss_to_severity
from grison.scanners.ir.cvss2 import cvss2_to_cvss3

from .base import ImportOptions, Scanner


def _parse_nvt_tags(tags_str: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in tags_str.split("|"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            result[k.strip()] = v.strip()
    return result


class OpenVASScanner(Scanner):
    name = "openvas"
    label = "OpenVAS"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)

        # Handle the get_reports_response wrapper: pick the first non-empty <results>
        # container (explicit len() check — Element truthiness is deprecated).
        results_parent = None
        for candidate in (
            root.find(".//report/report/results"),
            root.find(".//report/results"),
            root.find(".//results"),
        ):
            if candidate is not None and len(candidate):
                results_parent = candidate
                break
        if results_parent is None:
            return []

        aggregated: dict[str, dict] = {}

        for result in results_parent.findall("result"):
            nvt = result.find("nvt")
            if nvt is None:
                continue

            oid = nvt.get("oid", "")
            if not oid:
                continue

            if not self._plugin_allowed(oid, opts):
                continue

            qod_el = result.find("qod/value")
            qod = int(qod_el.text or "0") if qod_el is not None else 0
            if qod < opts.min_qod:
                continue

            try:
                score = float((result.findtext("severity") or "0").strip())
            except ValueError:
                score = 0.0
            severity = cvss_to_severity(score)

            if not self._severity_allowed(severity, opts):
                continue

            host_el = result.find("host")
            host_ip = (host_el.text or "").strip() if host_el is not None else ""
            hostname_el = result.find("host/hostname") if host_el is not None else None
            hostname = (hostname_el.text or "").strip() if hostname_el is not None else ""
            port = (result.findtext("port") or "").strip()

            component = hostname or host_ip
            if port and port.lower() not in ("general/tcp", ""):
                component += f":{port}"

            tags_raw = nvt.findtext("tags") or ""
            tags = _parse_nvt_tags(tags_raw)

            refs = [
                ref.get("id", "")
                for ref in nvt.findall(".//refs/ref")
                if ref.get("type") in ("cve", "url")
            ]

            if oid not in aggregated:
                # cvss_base_vector is bare CVSS v2 on older NVTs (v2 has no "CVSS:"
                # prefix in its own spec) but a properly prefixed v3 vector on
                # newer ones — convert only the former, leave the latter untouched.
                cvss_raw = tags.get("cvss_base_vector", "").strip()
                if cvss_raw and not cvss_raw.startswith("CVSS:"):
                    cvss_raw = cvss2_to_cvss3(cvss_raw)
                aggregated[oid] = {
                    "title": nvt.findtext("name") or oid,
                    "severity": severity,
                    "cvss_vector": cvss_raw,
                    "description": tags.get("summary") or result.findtext("description") or "",
                    "impact": tags.get("impact", ""),
                    "mitigation": tags.get("solution", ""),
                    "finding_guidance": tags.get("vuldetect", ""),
                    "refs": refs,
                    "affected": [component] if component else [],
                }
            else:
                if component and component not in aggregated[oid]["affected"]:
                    aggregated[oid]["affected"].append(component)

        findings: list[Finding] = []
        for oid, meta in aggregated.items():
            refs_html = (
                "<ul>"
                + "".join(f"<li>{r}</li>" for r in meta["refs"] if r)
                + "</ul>"
                if meta["refs"]
                else ""
            )
            findings.append(
                Finding(
                    title=meta["title"],
                    plugin_id=oid,
                    severity=meta["severity"],
                    cvss_vector=meta["cvss_vector"],
                    description=meta["description"],
                    impact=meta["impact"],
                    mitigation=meta["mitigation"],
                    finding_guidance=meta["finding_guidance"],
                    references=refs_html,
                    affected_components=meta["affected"],
                )
            )

        return self.sort_by_severity(findings)
