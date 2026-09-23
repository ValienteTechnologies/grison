"""CWE-id normalisation shared by scanners that emit a bare-ish CWE reference.

Scanners disagree on shape: a plain digit string ("79"), an already-prefixed
string ("CWE-79"), an int (JSON), or a sentinel meaning "no CWE" (Nessus/Qualys
leave the field empty; ZAP emits ``cweid`` "-1" or "0"; Acunetix sometimes emits
"CWE-" with nothing after it). This is IR-level normalisation only — the
finding-format layer (:mod:`grison.model.cwe`) separately validates the result
against the real CWE index.
"""

from __future__ import annotations


def normalize_cwe(raw: str | int | None) -> str:
    """Normalize a scanner-native CWE id to ``"CWE-N"``, or ``""`` if the value is
    missing, non-numeric, or one of the "no CWE" sentinels (``-1``, ``0``)."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.upper().startswith("CWE-"):
        s = s[4:].strip()
    if not s.lstrip("-").isdigit():
        return ""
    n = int(s)
    if n <= 0:
        return ""
    return f"CWE-{n}"
