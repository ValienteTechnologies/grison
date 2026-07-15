"""Scanner-type auto-detection — sniff a file to pick its parser.

The vendored parsers mostly don't gate on their document's root element, so
detection is a *separate* concern, built from each tool's native export root
rather than lifted from the parsers. JSON is distinguished by content (SSLyze vs
ZAP-JSON); XML by its root element. Unrecognized input returns ``None`` so the
pipeline can skip it with a warning.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as ET

# Native XML root element (local name) -> scanner slug. Case matters: Acunetix's
# `Scan` vs Qualys's all-caps `SCAN` are different roots.
_XML_ROOT_TO_SCANNER: dict[str, str] = {
    "NessusClientData_v2": "nessus",
    "issues": "burp",
    "nmaprun": "nmap",
    "OWASPZAPReport": "zap",
    "ScanGroup": "acunetix",
    "Scan": "acunetix",
    "WAS_SCAN_REPORT": "qualys",
    "SCAN": "qualys",
    "report": "openvas",
    "get_reports_response": "openvas",
    "results": "openvas",
}

# Enough to reach the root element / top-level JSON keys without reading a 2 MB file.
_HEAD_BYTES = 65536


def detect(path: Path) -> str | None:
    """Return the scanner slug for ``path``, or ``None`` if unrecognized."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    return detect_bytes(head)


def detect_bytes(data: bytes) -> str | None:
    """Sniff a scanner export from its leading bytes."""
    stripped = data.lstrip()
    if not stripped:
        return None
    first = stripped[:1]
    if first in (b"{", b"["):
        return _detect_json(data)
    if first == b"<":
        return _detect_xml(data)
    return None


def _detect_json(data: bytes) -> str | None:
    if b'"server_scan_results"' in data:
        return "sslyze"
    # ZAP can emit JSON too, though it's primarily XML in our workflows.
    if b'"site"' in data and (b'"@version"' in data or b'"@generated"' in data):
        return "zap"
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _detect_xml(data: bytes) -> str | None:
    # Pull-parse just far enough to see the root element's start tag, then stop —
    # a truncated head still yields the root 'start' event before any parse error.
    try:
        for _event, elem in ET.iterparse(BytesIO(data), events=("start",)):
            return _XML_ROOT_TO_SCANNER.get(_local_name(elem.tag))
    except ParseError:
        return None
    return None
