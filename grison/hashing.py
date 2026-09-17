"""One canonicalization for every content hash grison persists as a merge base.

:func:`digest` is the forward-looking standard: ``"sha256:" + sha256(json.dumps(
payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))`` — the compact
form, for any new caller.

Three existing call sites (``gwmap.content_hash``, ``bsmap.bs_content_hash``,
``repmap.section_hash``) predate this module and already persist their hash as a
3-way merge base in every synced workspace, computed with ``json.dumps(...,
sort_keys=True, ensure_ascii=False)`` and the *default* (non-compact) separators. If
routing them through :func:`digest`'s compact form changed a single byte, every
record in every existing workspace would look locally edited the moment this ships
(the change guard would trip workspace-wide). So those three route through
:func:`digest_legacy` (dict payloads, old separators) or :func:`digest_text` (a bare
string, no JSON envelope at all — ``repmap.section_hash`` hashes the section's
markdown directly) instead, both proven byte-identical to the pre-refactor output by
a golden test pinned *before* the refactor landed.

The two un-prefixed call sites (``gwmap.evidence_meta_hash``,
``methodology._mirror_hash``) never persisted a ``sha256:`` prefix; they now do,
via the same legacy formula (only the prefix is new — the hash *computation* stays
identical, so a stored value's hex digest still matches a freshly computed one).
:func:`normalize` is the one-time compatibility shim this creates: an existing
``.grison/state`` entry (or ``mirrors.json`` entry) holds the old, un-prefixed hex
digest; this grison always *writes* the prefixed form from now on; the comparison at
both call sites must treat the two spellings of the same hash as equal, or every
such record would falsely reclassify as changed on the first sync after upgrade.
It's deliberately the only compatibility allowance in this module — the later
workspace-migration step rewrites private state into the new format wholesale, and
this helper (and the need for it) goes away with it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_PREFIX = "sha256:"


def digest(payload: Any) -> str:
    """The canonical content hash: ``sha256:<hex>`` over ``payload`` dumped
    compactly (``sort_keys=True, ensure_ascii=False, separators=(",", ":")``). For
    new callers — no existing merge base was ever computed this way."""
    return _PREFIX + _sha256_json(payload, compact=True)


def digest_legacy(payload: Any) -> str:
    """Byte-identical to the pre-existing dict-payload merge-base hashes
    (``gwmap.content_hash``, ``bsmap.bs_content_hash``, and — after adding the
    prefix — ``gwmap.evidence_meta_hash``): ``sort_keys=True, ensure_ascii=False``,
    default (non-compact) JSON separators. See the module docstring."""
    return _PREFIX + _sha256_json(payload, compact=False)


def digest_text(text: str) -> str:
    """Byte-identical to the pre-existing bare-string merge-base hashes
    (``repmap.section_hash``, and — after adding the prefix —
    ``methodology._mirror_hash``): sha256 of ``text``'s UTF-8 bytes directly, no JSON
    envelope. Callers strip ``text`` themselves if that's part of their contract."""
    return _PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any, *, compact: bool) -> str:
    kwargs: dict[str, Any] = {"sort_keys": True, "ensure_ascii": False}
    if compact:
        kwargs["separators"] = (",", ":")
    text = json.dumps(payload, **kwargs)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(stored: str | None) -> str | None:
    """Treat an un-prefixed legacy hash as equal to its ``sha256:``-prefixed form
    for comparison — only ``evidence_meta_hash``/``_mirror_hash`` stored values can
    be un-prefixed; every other stored hash has always carried the prefix."""
    if stored is None or stored.startswith(_PREFIX):
        return stored
    return _PREFIX + stored
