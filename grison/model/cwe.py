"""CWE lookup: validate ``cwe`` field values against the embedded MITRE index
instead of treating them as free text.

The index (``data/cwe.json``, id -> name) is generated offline by
``scripts/gen_cwe_index.py`` from the official MITRE CWE catalog and loaded
once here via ``importlib.resources``.
"""

from __future__ import annotations

import importlib.resources
import json
import re

_CWE_ID_RE = re.compile(r"^(?:CWE-)?0*(\d+)$", re.IGNORECASE)

_DATA_PATH = importlib.resources.files("grison.model") / "data" / "cwe.json"
_CWE_INDEX: dict[str, str] = json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def normalize_cwe(raw: str) -> str | None:
    """Normalize a raw CWE identifier to canonical ``CWE-<n>`` form.

    Accepts "16", "CWE-16", "cwe-16", " CWE-0016 " -> "CWE-16". Returns None
    for anything that doesn't parse as a CWE id.
    """
    if not isinstance(raw, str):
        return None
    match = _CWE_ID_RE.match(raw.strip())
    if match is None:
        return None
    return f"CWE-{int(match.group(1))}"


def cwe_name(cwe_id: str) -> str | None:
    """Look up the name for a CWE id (any accepted raw form). None if unknown."""
    normalized = normalize_cwe(cwe_id)
    if normalized is None:
        return None
    numeric_key = normalized.removeprefix("CWE-")
    return _CWE_INDEX.get(numeric_key)


def is_known_cwe(cwe_id: str) -> bool:
    """True iff ``cwe_id`` normalizes to a valid CWE id present in the index."""
    return cwe_name(cwe_id) is not None
