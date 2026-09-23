"""CVSS 2.0 → 3.1 best-effort vector conversion, shared by scanners whose
native XML only carries a v2 vector (Nessus's ``cvss_vector``, OpenVAS's
``cvss_base_vector`` NVT tag when it predates the scanner's v3 rollout).

CVSS v2 encodes less information than v3 (no distinct UI metric, no scope) —
this is a lossy, best-effort mapping, not a reconstruction of the original
v3 assessment.
"""

from __future__ import annotations

from grison.errors import GrisonError

# Static CVSS2 → CVSS3.1 field mapping used for vector conversion
_AV_MAP = {"L": "L", "A": "A", "N": "N"}
_AC_MAP = {"L": "L", "M": "H", "H": "H"}
_AU_TO_PR = {"N": "N", "S": "L", "M": "H"}
_CIA_MAP = {"N": "N", "P": "L", "C": "H"}


class CvssConversionError(GrisonError, ValueError):
    """Raised by :func:`cvss2_to_cvss3` when ``v2`` doesn't parse as a CVSS2
    vector at all (no recognizable ``KEY:VALUE`` pairs) — a caller must not
    silently substitute an all-defaults vector for this; see each caller's own
    ``except`` for how it's surfaced (empty ``cvss_vector`` plus a dropped-note
    warning, matching the CVSS v4-only case)."""


def cvss2_to_cvss3(v2: str) -> str:
    """Best-effort CVSS2 → CVSS3.1 vector string conversion.

    Raises :class:`CvssConversionError` if ``v2`` carries no recognizable
    ``KEY:VALUE`` metric pairs at all — never silently returns an all-defaults
    vector for a string that isn't a CVSS2 vector in the first place. A vector
    that does parse but omits or misspells an individual metric still falls
    back to that metric's CVSS3 default (e.g. a missing ``Au`` becomes
    ``PR:N``) — CVSS2 exports in the wild are inconsistent about which metrics
    they include, and that partial-data case is the best-effort conversion
    this function exists for.
    """
    # Scanners emit several shapes: "CVSS2#AV:N/...", bare "AV:N/...", and
    # "(AV:N/...)". Strip the "CVSS2#" prefix (if any) and any surrounding
    # parens/whitespace before splitting, so "AV:" survives as a real key.
    vec = v2.split("#", 1)[-1].strip().strip("()")
    parts = dict(p.split(":", 1) for p in vec.split("/") if ":" in p)
    if not parts:
        raise CvssConversionError(f"not a CVSS v2 vector: {v2!r}")
    av = _AV_MAP.get(parts.get("AV", ""), "N")
    ac = _AC_MAP.get(parts.get("AC", ""), "L")
    pr = _AU_TO_PR.get(parts.get("Au", ""), "N")
    ui = "N"
    scope = "U"
    c = _CIA_MAP.get(parts.get("C", ""), "N")
    i = _CIA_MAP.get(parts.get("I", ""), "N")
    a = _CIA_MAP.get(parts.get("A", ""), "N")
    return f"CVSS:3.1/AV:{av}/AC:{ac}/PR:{pr}/UI:{ui}/S:{scope}/C:{c}/I:{i}/A:{a}"


def ensure_cvss3_prefix(vector: str) -> str:
    """Prepend a ``CVSS:3.0/`` prefix to a bare CVSS3 vector string.

    Several scanners (Nessus's ``cvss3_vector``, Qualys WAS's
    ``CVSS_V3/VECTOR_STRING``) emit an already-v3 vector without its ``CVSS:3.x/``
    header. This is not a v2->v3 conversion — the vector is real v3 data, just
    missing its prefix — so a vector that already carries one is returned
    untouched.
    """
    vector = vector.strip()
    if not vector:
        return ""
    return vector if vector.startswith("CVSS:3") else f"CVSS:3.0/{vector}"
