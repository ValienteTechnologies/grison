from __future__ import annotations

import re
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET

from grison.scanners.ir import ScanFinding, Severity
from grison.scanners.ir.cvss2 import ensure_cvss3_prefix
from grison.scanners.ir.severity import severity_or_info

from .base import (
    AggregatedRecord,
    Aggregator,
    FindingReferences,
    ImportOptions,
    RawOccurrence,
    Scanner,
    refs_to_html,
)

_SEVERITY_MAP = {
    "1": Severity.INFO,
    "2": Severity.LOW,
    "3": Severity.MEDIUM,
    "4": Severity.HIGH,
    "5": Severity.CRITICAL,
}

# A VM (ASSET_DATA_REPORT) glossary's CVSS3_SCORE/CVSS3_BASE carries the score
# and vector together as "9.0 (AV:N/AC:H/PR:N/UI:N/S:C/C:H/I:H/A:H)" — pull the
# parenthesised vector back out.
_CVSS3_VECTOR_IN_PARENS = re.compile(r"\(([^()]+)\)")


def _parse_severity(raw: str) -> Severity:
    mapped = _SEVERITY_MAP.get(raw)
    if mapped is not None:
        return mapped
    # Qualys severities are numeric in practice; this only fires for an
    # unrecognised code.
    return severity_or_info(raw)


def _ref_entry(ref_id: str, url: str) -> str | tuple[str, str]:
    """One CVE/vendor reference as a bare id (no url) or an (id, url) pair —
    the shape refs_to_html/Aggregator expect (see FindingReferences)."""
    return (ref_id, url) if url else ref_id


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
        elif tag == "ASSET_DATA_REPORT":
            return self._parse_vm(root, opts)
        else:
            raise ValueError(
                f"Unrecognised Qualys root element: {tag!r}. "
                "Expected WAS_SCAN_REPORT, SCAN, or ASSET_DATA_REPORT."
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
                    "group": qid_el.findtext("GROUP") or "",
                    "description": qid_el.findtext("DESCRIPTION") or "",
                    "impact": qid_el.findtext("IMPACT") or "",
                    "solution": qid_el.findtext("SOLUTION") or "",
                    "cvss_vector": cvss_vector,
                    "cve_list": [c.text or "" for c in qid_el.findall(".//CVE_LIST/CVE/ID")],
                }

        agg = Aggregator()
        # A WAS report's findings split across two lists under the same RESULTS
        # element — confirmed vulnerabilities and "information gathered" (headers,
        # crawl stats, and similar recon-ish items DefectDojo's qualys_webapp
        # parser still turns into findings, resolved from the same glossary by
        # QID). `.//RESULTS/...` (not `./RESULTS/...`) matches both the flat
        # placement our corpus fixtures use and the WAS_WEBAPP_REPORT root's
        # nested RESULTS/WEB_APPLICATION/... placement, for free.
        self._collect_was_items(
            root, ".//RESULTS/VULNERABILITY_LIST/VULNERABILITY", glossary, agg, opts
        )
        self._collect_was_items(
            root, ".//RESULTS/INFORMATION_GATHERED_LIST/INFORMATION_GATHERED", glossary, agg, opts
        )

        return self._finish(agg)

    def _collect_was_items(
        self,
        root: Element,
        xpath: str,
        glossary: dict[str, dict],
        agg: Aggregator,
        opts: ImportOptions,
    ) -> None:
        for item in root.findall(xpath):
            qid = item.findtext("QID") or ""
            url = item.findtext("URL") or ""  # INFORMATION_GATHERED items carry none
            meta = glossary.get(qid, {"title": f"QID {qid}", "severity": "3"})

            severity = _parse_severity(str(meta.get("severity", "3")))
            # The glossary GROUP sorts "information gathered" QIDs into
            # scan-diagnostic noise (IG-DIAG: connection errors, OS detection,
            # crawl/scan stats — e.g. QID 150018 "Connection Error Occurred
            # During Web Application Scan", QID 45017 "Operating System
            # Detected") versus reportable weaknesses (IG-WEAK: missing
            # security headers, cookie issues, HSTS...). DefectDojo's own
            # qualys_webapp parser flattens every information-gathered item to
            # Info by default; we go narrower and only force IG-DIAG items to
            # INFO, since IG-WEAK items (and all VULNERABILITY_LIST items,
            # which never carry an IG-* group) are real, reportable findings
            # whose numeric severity should stand. Older WAS exports spell the
            # group without the "IG-" prefix (bare "DIAG"/"WEAK", see the reptor
            # fixture), so both spellings count.
            group = str(meta.get("group", ""))
            if group.startswith("IG-DIAG") or group == "DIAG":
                severity = Severity.INFO
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

    def _parse_vm(self, root: Element, opts: ImportOptions) -> list[ScanFinding]:
        # The VM (vulnerability-management) export: HOST_LIST/HOST/VULN_INFO_LIST/
        # VULN_INFO per host, resolved against a GLOSSARY/VULN_DETAILS_LIST keyed
        # by QID for the shared prose/severity/CVSS/reference fields — same
        # glossary-lookup shape as _parse_was above, different XML dialect.
        glossary = self._build_vm_glossary(root)
        agg = Aggregator()

        for host in root.findall(".//HOST_LIST/HOST"):
            ip = host.findtext("IP") or ""
            dns = host.findtext("DNS") or ""
            label = dns or ip

            for vuln in host.findall(".//VULN_INFO_LIST/VULN_INFO"):
                qid = vuln.findtext("QID") or ""
                if not qid:
                    continue
                meta = glossary.get(qid, {"title": f"QID {qid}", "severity": "3"})

                severity = _parse_severity(str(meta.get("severity", "3")))
                if not self._severity_allowed(severity, opts):
                    continue
                if not self._plugin_allowed(qid, opts):
                    continue

                port = vuln.findtext("PORT")
                component = f"{label}:{port}" if label and port else label

                agg.add(
                    RawOccurrence(
                        key=qid,
                        title=meta.get("title", f"QID {qid}"),
                        severity=severity,
                        affected_component=component,
                        cvss_vector=meta.get("cvss_vector", ""),
                        description=meta.get("description", ""),
                        impact=meta.get("impact", ""),
                        mitigation=meta.get("solution", ""),
                        references=list(meta.get("references", [])),
                    )
                )

        return self._finish(agg)

    def _build_vm_glossary(self, root: Element) -> dict[str, dict]:
        glossary: dict[str, dict] = {}
        for vd in root.findall(".//GLOSSARY/VULN_DETAILS_LIST/VULN_DETAILS"):
            qid = vd.findtext("QID") or ""
            if not qid:
                continue

            cvss_vector = ""
            cvss3_base = vd.findtext("CVSS3_SCORE/CVSS3_BASE") or ""
            m = _CVSS3_VECTOR_IN_PARENS.search(cvss3_base)
            if m:
                cvss_vector = ensure_cvss3_prefix(m.group(1))

            references: FindingReferences = []
            for cve in vd.findall("CVE_ID_LIST/CVE_ID"):
                cve_id = cve.findtext("ID") or ""
                if cve_id:
                    references.append(_ref_entry(cve_id, cve.findtext("URL") or ""))
            for vendor_ref in vd.findall("VENDOR_REFERENCE_LIST/VENDOR_REFERENCE"):
                vendor_id = vendor_ref.findtext("ID") or ""
                if vendor_id:
                    references.append(_ref_entry(vendor_id, vendor_ref.findtext("URL") or ""))

            glossary[qid] = {
                "title": vd.findtext("TITLE") or f"QID {qid}",
                "severity": vd.findtext("SEVERITY") or "3",
                # THREAT/IMPACT are this schema's actual field names for the
                # vulnerability's description and consequence text (there is no
                # DIAGNOSIS/CONSEQUENCE pair here — that's the sibling SCAN-root
                # dialect _parse_vuln handles above). DefectDojo's own
                # dojo/tools/qualys/parser.py maps the same two fields the same
                # way (temp["vuln_description"] from THREAT, temp["IMPACT"] from
                # IMPACT).
                "description": vd.findtext("THREAT") or "",
                "impact": vd.findtext("IMPACT") or "",
                "solution": vd.findtext("SOLUTION") or "",
                "cvss_vector": cvss_vector,
                "references": references,
            }
        return glossary

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
