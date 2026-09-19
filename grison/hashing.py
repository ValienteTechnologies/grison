"""One canonicalization for every content hash grison persists as a merge base.

:func:`digest` is the standard: ``"sha256:" + sha256(json.dumps(payload,
sort_keys=True, ensure_ascii=False, separators=(",", ":")))`` — the compact form,
used by every current caller (the engine's own state layer, the validator's mirror
digests, scaffold's idempotency checks).

:func:`digest_text` is the bare-string form (sha256 of the text's UTF-8 bytes
directly, no JSON envelope) — for callers hashing a string that already IS the
payload (e.g. a mirror file's raw text) rather than a JSON-shaped record.

Workspace format v1's three call sites that predated this module (``gwmap.
content_hash``, ``bsmap.bs_content_hash``, ``repmap.section_hash``) — and the
un-prefixed-legacy-hash compatibility shim (``digest_legacy``/``normalize``) that
existed only to keep an old persisted value's bytes stable across the refactor that
introduced this module — are gone along with format v1 itself: D13 refuses an
old-format workspace outright rather than converting it in place, so no workspace
this code ever touches can hold one of those old un-prefixed values.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_PREFIX = "sha256:"


def digest(payload: Any) -> str:
    """The canonical content hash: ``sha256:<hex>`` over ``payload`` dumped
    compactly (``sort_keys=True, ensure_ascii=False, separators=(",", ":")``)."""
    return _PREFIX + _sha256_json(payload, compact=True)


def digest_text(text: str) -> str:
    """sha256 of ``text``'s UTF-8 bytes directly, no JSON envelope. Callers strip
    ``text`` themselves if that's part of their contract."""
    return _PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any, *, compact: bool) -> str:
    kwargs: dict[str, Any] = {"sort_keys": True, "ensure_ascii": False}
    if compact:
        kwargs["separators"] = (",", ":")
    text = json.dumps(payload, **kwargs)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
