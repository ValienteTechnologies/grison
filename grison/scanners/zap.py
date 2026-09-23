from __future__ import annotations

import html
import json

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding, Severity
from grison.scanners.ir.cwe import normalize_cwe
from grison.scanners.ir.severity import severity_or_info

from .base import AggregatedRecord, Aggregator, ImportOptions, RawOccurrence, Scanner, refs_to_html

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

# ZAP's own per-alert confidence scale (a `<confidence>` element distinct from
# riskcode/severity): 0..3 -> false-positive/low/medium/high, ZAP's own scale
# (see ZAP's Alert.Confidence and DefectDojo's zap parser MAPPING_CONFIDENCE).
# Surfaced as a `confidence:<value>` tag rather than a new IR field — workspace
# format v2 has no confidence field, and a tag is the least invasive carrier.
_CONFIDENCE_MAP = {
    "0": "false-positive",
    "1": "low",
    "2": "medium",
    "3": "high",
}
_CONFIDENCE_RANK = {"false-positive": 0, "low": 1, "medium": 2, "high": 3}


class ZapScanner(Scanner):
    name = "zap"
    label = "OWASP ZAP"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        # Auto-detect format: try JSON first, fall back to XML
        text = data.decode("utf-8", errors="replace").lstrip()
        if text.startswith("{") or text.startswith("["):
            return self._parse_json(data, opts)
        return self._parse_xml(data, opts)

    def _parse_json(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        doc = json.loads(data)
        sites = doc if isinstance(doc, list) else doc.get("site", [])
        if isinstance(sites, dict):
            sites = [sites]

        agg = Aggregator()
        best_confidence: dict[str, str] = {}
        for site in sites:
            for alert in site.get("alerts", []):
                self._collect(alert, agg, opts, best_confidence)

        findings = [self._to_finding(rec, best_confidence.get(rec.key)) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _parse_xml(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        root = ET.fromstring(data)
        agg = Aggregator()
        best_confidence: dict[str, str] = {}

        for alert_el in root.findall(".//alertitem"):
            alert: dict = {}
            for child in alert_el:
                if child.tag == "instances":
                    alert["instances"] = [
                        {gc.tag: (gc.text or "").strip() for gc in instance}
                        for instance in child.findall("instance")
                    ]
                else:
                    alert[child.tag] = (child.text or "").strip()
            self._collect(alert, agg, opts, best_confidence)

        findings = [self._to_finding(rec, best_confidence.get(rec.key)) for rec in agg.records()]
        return self.sort_by_severity(findings)

    def _collect(
        self, alert: dict, agg: Aggregator, opts: ImportOptions, best_confidence: dict[str, str]
    ) -> None:
        alert_ref = alert.get("alertRef") or alert.get("pluginid") or alert.get("id", "")
        severity = self._severity_for(alert)

        if not self._severity_allowed(severity, opts):
            return
        if not self._plugin_allowed(alert_ref, opts):
            return

        # Highest confidence seen per alert_ref — Aggregator itself only keeps the
        # first occurrence's fields, so a later, more-confident occurrence of the
        # same alert needs tracking separately (see module comment above).
        conf_tag = _CONFIDENCE_MAP.get(str(alert.get("confidence", "")).strip())
        if conf_tag is not None:
            current = best_confidence.get(alert_ref)
            if current is None or _CONFIDENCE_RANK[conf_tag] > _CONFIDENCE_RANK[current]:
                best_confidence[alert_ref] = conf_tag

        instances = alert.get("instances", [])
        uris = [inst.get("uri", "") for inst in instances if inst.get("uri")]

        # Every instance field lands in an HTML fragment below (<li>...) — a ZAP
        # export's uri/method/param can themselves carry an XSS payload string
        # (the vendor is faithfully quoting what it sent), which must not be
        # allowed to become real markup here; html.escape every one of them.
        rep_steps = ""
        if instances:
            rows = "".join(
                f"<li>{html.escape(inst.get('uri', ''))} "
                f"[{html.escape(inst.get('method', 'GET'))}]"
                + (
                    f" param: <code>{html.escape(inst.get('param', ''))}</code>"
                    if inst.get("param")
                    else ""
                )
                + "</li>"
                for inst in instances[:20]
            )
            rep_steps = f"<ul>{rows}</ul>"

        ref_raw = alert.get("reference", alert.get("references", ""))
        refs = (
            [line.strip() for line in ref_raw.replace("\r", "").split("\n") if line.strip()]
            if ref_raw
            else []
        )

        cwe = normalize_cwe(alert.get("cweid"))
        description = alert.get("desc", alert.get("description", ""))
        mitigation = alert.get("solution", "")
        title = alert.get("name", alert.get("alert", f"Alert {alert_ref}"))

        # One occurrence per instance URI (or a single componentless occurrence if
        # there are none) so the aggregator's own dedup/merge handles multi-site
        # alerts the same way every other parser's aggregation does; fields are
        # identical across all of an alert's own instances, so which occurrence
        # "wins" them doesn't matter.
        for uri in uris or [""]:
            agg.add(
                RawOccurrence(
                    key=alert_ref,
                    title=title,
                    severity=severity,
                    affected_component=uri,
                    cwe=cwe,
                    description=description,
                    mitigation=mitigation,
                    references=refs,
                    replication_steps=rep_steps,
                )
            )

    def _severity_for(self, alert: dict) -> Severity:
        riskcode = alert.get("riskcode")
        code = str(riskcode)[:1] if riskcode not in (None, "") else ""
        if code in _RISKCODE_MAP:
            return _RISKCODE_MAP[code]

        riskdesc = str(alert.get("riskdesc", ""))
        word = riskdesc.split("(", 1)[0].strip().lower()
        mapped_code = _RISKDESC_WORD_MAP.get(word)
        if mapped_code is not None:
            return _RISKCODE_MAP[mapped_code]
        return severity_or_info(word)

    def _to_finding(self, rec: AggregatedRecord, confidence: str | None = None) -> ScanFinding:
        tags = [f"confidence:{confidence}"] if confidence else []
        return ScanFinding(
            title=rec.title,
            plugin_id=rec.key,
            severity=rec.severity,
            cwe=rec.cwe,
            description=rec.description,
            mitigation=rec.mitigation,
            references=refs_to_html(rec.references),
            replication_steps=rec.replication_steps,
            affected_components=rec.affected_components,
            tags=tags,
        )
