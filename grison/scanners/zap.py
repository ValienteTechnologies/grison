from __future__ import annotations

import html
import json

import defusedxml.ElementTree as ET

from grison.scanners.ir import Finding, Severity

from .base import ImportOptions, Scanner

# ZAP's native riskcode scale is 0=Informational, 1=Low, 2=Medium, 3=High.
# ZAP has no Critical tier.
_RISKCODE_MAP = {
    "3": Severity.HIGH,
    "2": Severity.MEDIUM,
    "1": Severity.LOW,
    "0": Severity.INFO,
}

# Fallback when riskcode is missing/unrecognized: derive the code from the
# leading word of riskdesc (e.g. "High (Medium)" -> "High"), so both paths
# agree on severity instead of riskdesc silently degrading to INFO.
_RISKDESC_WORD_MAP = {
    "informational": "0",
    "low": "1",
    "medium": "2",
    "high": "3",
}


class ZapScanner(Scanner):
    name = "zap"
    label = "OWASP ZAP"

    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        # Auto-detect format: try JSON first, fall back to XML
        text = data.decode("utf-8", errors="replace").lstrip()
        if text.startswith("{") or text.startswith("["):
            return self._parse_json(data, opts)
        return self._parse_xml(data, opts)

    def _parse_json(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        doc = json.loads(data)
        sites = doc if isinstance(doc, list) else doc.get("site", [])
        if isinstance(sites, dict):
            sites = [sites]

        aggregated: dict[str, dict] = {}
        for site in sites:
            alerts = site.get("alerts", [])
            for alert in alerts:
                self._aggregate(alert, aggregated, opts)

        return self._to_findings(aggregated)

    def _parse_xml(self, data: bytes, opts: ImportOptions) -> list[Finding]:
        root = ET.fromstring(data)
        aggregated: dict[str, dict] = {}

        for alert_el in root.findall(".//alertitem"):
            alert: dict = {}
            for child in alert_el:
                if child.tag == "instances":
                    alert["instances"] = [
                        {
                            gc.tag: (gc.text or "").strip()
                            for gc in instance
                        }
                        for instance in child.findall("instance")
                    ]
                else:
                    alert[child.tag] = (child.text or "").strip()
            self._aggregate(alert, aggregated, opts)

        return self._to_findings(aggregated)

    def _aggregate(
        self, alert: dict, aggregated: dict[str, dict], opts: ImportOptions
    ) -> None:
        alert_ref = alert.get("alertRef") or alert.get("pluginid") or alert.get("id", "")
        severity = self._severity_for(alert)

        if not self._severity_allowed(severity, opts):
            return
        if not self._plugin_allowed(alert_ref, opts):
            return

        instances = alert.get("instances", [])
        uris = [inst.get("uri", "") for inst in instances if inst.get("uri")]

        if alert_ref not in aggregated:
            ref_raw = alert.get("reference", alert.get("references", ""))
            refs_html = self._refs_to_html(ref_raw)

            rep_steps = ""
            if instances:
                rows = "".join(
                    f"<li>{inst.get('uri', '')} "
                    f"[{inst.get('method', 'GET')}]"
                    + (f" param: <code>{inst.get('param', '')}</code>" if inst.get("param") else "")
                    + "</li>"
                    for inst in instances[:20]
                )
                rep_steps = f"<ul>{rows}</ul>"

            # cweid of "-1" (unmapped) or "0" is a ZAP sentinel, not a real CWE ID.
            cweid = alert.get("cweid")
            cweid_str = str(cweid).strip() if cweid not in (None, "") else ""
            cwe = "" if cweid_str in ("", "-1", "0") else cweid_str

            aggregated[alert_ref] = {
                "title": alert.get("name", alert.get("alert", f"Alert {alert_ref}")),
                "severity": severity,
                "cwe": cwe,
                "description": alert.get("desc", alert.get("description", "")),
                "mitigation": alert.get("solution", ""),
                "references": refs_html,
                "replication_steps": rep_steps,
                "affected": list(dict.fromkeys(uris)),
            }
        else:
            aggregated[alert_ref]["severity"] = self.max_severity(
                aggregated[alert_ref]["severity"], severity
            )
            for uri in uris:
                if uri and uri not in aggregated[alert_ref]["affected"]:
                    aggregated[alert_ref]["affected"].append(uri)

    def _severity_for(self, alert: dict) -> Severity:
        riskcode = alert.get("riskcode")
        code = str(riskcode)[:1] if riskcode not in (None, "") else ""
        if code not in _RISKCODE_MAP:
            riskdesc = str(alert.get("riskdesc", ""))
            word = riskdesc.split("(", 1)[0].strip().lower()
            code = _RISKDESC_WORD_MAP.get(word, "")
        return _RISKCODE_MAP.get(code, Severity.INFO)

    def _refs_to_html(self, raw: str) -> str:
        if not raw:
            return ""
        lines = [line.strip() for line in raw.replace("\r", "").split("\n") if line.strip()]
        items = "".join(
            f'<li><a href="{html.escape(line, quote=True)}">{html.escape(line)}</a></li>'
            if line.startswith("http")
            else f"<li>{html.escape(line)}</li>"
            for line in lines
        )
        return f"<ul>{items}</ul>" if items else ""

    def _to_findings(self, aggregated: dict[str, dict]) -> list[Finding]:
        findings = [
            Finding(
                title=meta["title"],
                plugin_id=alert_ref,
                severity=meta["severity"],
                cwe=f"CWE-{meta['cwe']}" if meta.get("cwe") else "",
                description=meta["description"],
                mitigation=meta["mitigation"],
                references=meta["references"],
                replication_steps=meta["replication_steps"],
                affected_components=meta["affected"],
            )
            for alert_ref, meta in aggregated.items()
        ]
        return self.sort_by_severity(findings)
