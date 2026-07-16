"""CVSS 2.0 → 3.1 best-effort vector conversion, shared by scanners whose
native XML only carries a v2 vector (Nessus's ``cvss_vector``, OpenVAS's
``cvss_base_vector`` NVT tag when it predates the scanner's v3 rollout).

CVSS v2 encodes less information than v3 (no distinct UI metric, no scope) —
this is a lossy, best-effort mapping, not a reconstruction of the original
v3 assessment.
"""

from __future__ import annotations

# Static CVSS2 → CVSS3.1 field mapping used for vector conversion
_AV_MAP = {"L": "L", "A": "A", "N": "N"}
_AC_MAP = {"L": "L", "M": "H", "H": "H"}
_AU_TO_PR = {"N": "N", "S": "L", "M": "H"}
_CIA_MAP = {"N": "N", "P": "L", "C": "H"}


def cvss2_to_cvss3(v2: str) -> str:
    """Best-effort CVSS2 → CVSS3.1 vector string conversion."""
    try:
        # Scanners emit several shapes: "CVSS2#AV:N/...", bare "AV:N/...", and
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
