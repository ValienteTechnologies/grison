"""Scanner-type auto-detection — sniff a file to pick its parser.

The vendored parsers mostly don't gate on their document's root element, so
detection is a *separate* concern, built from each tool's native export root
rather than lifted from the parsers. JSON is distinguished by content (SSLyze vs
ZAP-JSON); XML by its root element. Unrecognized input returns ``None`` so the
pipeline can skip it with a warning.
"""

from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as ET

# Native XML root element (local name) -> scanner slug. Case matters: Acunetix's
# `Scan` vs Qualys's all-caps `SCAN` are different roots. The three entries also
# listed in _GENERIC_XML_ROOTS below are ambiguous by root name alone and go
# through _resolve_generic_root instead of this direct lookup.
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

# Root element names generic enough that an unrelated document could plausibly
# use them too ("report", "results" for openvas; "issues" for burp) — these need
# a second marker (an attribute or a nearby child) before we commit to a scanner,
# unlike e.g. "nmaprun" or "NessusClientData_v2", which are unique on their own.
_GENERIC_XML_ROOTS = {"report", "results", "issues"}

# Children seen within the first few elements of a genuine OpenVAS/GVM <report>
# or <results> export (its GMP envelope, or a nested <report> carrying its own
# <results>) — see openvas_sample.xml and the dojo/reptor corpus.
_OPENVAS_CHILD_MARKERS = {"report_format", "results", "omp", "result"}

# How many elements past the root to peek at when a root is generic — enough to
# see an OpenVAS report's near children without walking a whole large document.
_MAX_PEEK_ELEMENTS = 200

# Enough to reach the root element / top-level JSON keys without reading a 2 MB file.
_HEAD_BYTES = 65536

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16 = (b"\xff\xfe", b"\xfe\xff")  # LE, BE


class InputEncodingError(ValueError):
    """A UTF-16-BOM'd input's bytes don't actually decode as UTF-16 (a lone
    surrogate, an odd-length buffer, or similar corruption). Raised by
    :func:`normalise_input`; :func:`grison.sinks.pipeline.run_parse` catches it
    the same way it catches a read ``OSError`` — skip the file, record it in
    ``errors``, keep processing the rest of the batch."""


def detect(path: Path) -> str | None:
    """Return the scanner slug for ``path``, or ``None`` if unrecognized."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    return detect_bytes(head)


def normalise_input(data: bytes) -> bytes:
    """Normalise a scanner export's raw bytes ahead of both sniffing and parsing.

    Strips a UTF-8 BOM, transcodes UTF-16 (LE or BE, picked from its BOM) to
    UTF-8, and drops leading whitespace — some vendor exports (the reptor Qualys
    corpus) start with a stray blank line before the XML declaration, which XML
    parsers reject outright since a declaration must be the very first thing in
    the document. Callers must run this once and feed the *same* result to both
    ``detect_bytes``/``detect`` and the chosen parser, so what gets sniffed is
    what gets parsed.

    The UTF-16 decode is strict, not lossy: a lone surrogate or an odd-length
    buffer (both signs of a genuinely corrupt export, not just an unusual one)
    raises :class:`InputEncodingError` rather than silently substituting U+FFFD
    for the bad bytes and feeding a scanner parser a document that silently
    doesn't say what the original bytes said.
    """
    if data.startswith(_BOM_UTF8):
        data = data[len(_BOM_UTF8) :]
    elif data[:2] in _BOM_UTF16:
        try:
            data = data.decode("utf-16", errors="strict").encode("utf-8")
        except UnicodeDecodeError as e:
            raise InputEncodingError(f"invalid UTF-16: {e}") from e
    return data.lstrip()


def detect_bytes(data: bytes) -> str | None:
    """Sniff a scanner export from its leading bytes.

    ``data`` should already be normalised (see :func:`normalise_input`); this
    also tolerates raw, un-normalised bytes for callers (like the golden suite)
    that deliberately sniff and parse without going through the pipeline.
    """
    stripped = normalise_input(data)
    if not stripped:
        return None
    first = stripped[:1]
    if first in (b"{", b"["):
        return _detect_json(stripped)
    if first == b"<":
        return _detect_xml(stripped)
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


def _resolve_generic_root(root: str, attrib: Mapping[str, str], children: set[str]) -> str | None:
    if root == "issues":
        # Burp's own DTD declares burpVersion on <issues>; an unrelated <issues>
        # root won't carry it.
        return "burp" if "burpVersion" in attrib else None
    if root in ("report", "results"):
        if "id" in attrib or (children & _OPENVAS_CHILD_MARKERS):
            return "openvas"
        return None
    return None


def _detect_xml(data: bytes) -> str | None:
    # Pull-parse just far enough to see the root element's start tag, then stop —
    # a truncated head still yields the root 'start' event before any parse
    # error. For an unambiguous root that's enough; a generic root (see
    # _GENERIC_XML_ROOTS) needs a peek at a bounded number of further elements
    # for a disambiguating marker, so we keep pulling (still bounded, and still
    # tolerant of the head chunk running out mid-element).
    root_tag: str | None = None
    root_attrib: Mapping[str, str] = {}
    children: set[str] = set()
    try:
        for i, (_event, elem) in enumerate(ET.iterparse(BytesIO(data), events=("start",))):
            local = _local_name(elem.tag)
            if root_tag is None:
                root_tag = local
                if local not in _GENERIC_XML_ROOTS:
                    return _XML_ROOT_TO_SCANNER.get(local)
                root_attrib = elem.attrib
                continue
            children.add(local)
            if i >= _MAX_PEEK_ELEMENTS:
                break
    except ParseError:
        pass
    if root_tag is None:
        return None
    return _resolve_generic_root(root_tag, root_attrib, children)
