from __future__ import annotations

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner

# Static CVSS2 → CVSS3.1 field mapping used for vector conversion
_AV_MAP = {"L": "L", "A": "A", "N": "N"}
_AC_MAP = {"L": "L", "M": "H", "H": "H"}
_AU_TO_PR = {"N": "N", "S": "L", "M": "H"}
_CIA_MAP = {"N": "N", "P": "L", "C": "H"}


def _cvss2_to_cvss3(v2: str) -> str:
    """Best-effort CVSS2 → CVSS3.1 vector string conversion."""
    try:
        # Nessus emits three shapes: "CVSS2#AV:N/...", bare "AV:N/...", and
        # "(AV:N/...)". Strip the "CVSS2#" prefix (if any) and any surrounding
        # parens/whitespace before splitting, so "AV:" survives as a real key.
        vec = v2.split("#", 1)[-1].strip().strip("()")
        parts = dict(p.split(":", 1) for p in vec.split("/") if ":" in p)
        av = _AV_MAP.get(parts.get("AV", ""), "N")
        ac = _AC_MAP.get(parts.get("AC", ""), "L")
        pr = _AU_TO_PR.get(parts.get("Au", ""), "N")
        ui = "N"
        scope = "U"
        c = _CIA_MAP.get(parts.get("C", ""), "N")
        i = _CIA_MAP.get(parts.get("I", ""), "N")
        a = _CIA_MAP.get(parts.get("A", ""), "N")
        return f"CVSS:3.1/AV:{av}/AC:{ac}/PR:{pr}/UI:{ui}/S:{scope}/C:{c}/I:{i}/A:{a}"
    except Exception:
        return ""


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
                    cvss_raw = item.findtext("cvss_vector") or item.findtext("cvss3_vector") or ""
                    if cvss_raw and not cvss_raw.startswith("CVSS:3"):
                        cvss_raw = _cvss2_to_cvss3(cvss_raw)

                    see_also_raw = (item.findtext("see_also") or "").strip()
                    refs = [u.strip() for u in see_also_raw.splitlines() if u.strip()]

                    aggregated[plugin_id] = {
                        "title": item.get("pluginName", f"Plugin {plugin_id}"),
                        "severity": severity,
                        "cvss_vector": cvss_raw,
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
                    description=description.strip(),
                    mitigation=meta["mitigation"],
                    references=refs_html,
                    affected_components=meta["affected"],
                )
            )

        return self.sort_by_severity(findings)
