"""Proofs for grison/hashing.py.

The golden-value test below reconstructs the pre-refactor formula independently
(inline ``json.dumps``/``hashlib``, never calling into ``grison.hashing`` itself) and
asserts it against the real call site's live output — the proof that routing
``repmap.section_hash`` through ``grison.hashing`` did not change a single persisted
byte. (``bsmap.bs_content_hash``'s own golden test was removed with
``grison/remote/bsmap.py`` — the wiki moved onto the sync engine, whose own state
layer always writes fresh ``grison.hashing.digest`` values, so there is no
v1-persisted byte left to stay compatible with. ``gwmap.content_hash``'s golden test
was removed the same way when findings moved onto the engine —
``grison/remote/gwmap.py`` and ``grison/remote/sync.py`` are gone; the engine's own
merge-base hashes are computed by :mod:`grison.adapters.gw_findings` via
``grison.hashing.digest`` directly, with no v1-persisted byte to stay compatible
with either.)
"""

from __future__ import annotations

import hashlib
import json

from grison import hashing
from grison.model.finding import EvidenceGwRef, EvidenceItem
from grison.remote.repmap import section_hash

# --- digest / digest_legacy / digest_text — direct behavior --------------------


def test_digest_uses_compact_separators() -> None:
    assert hashing.digest({"b": 1, "a": 2}) == (
        "sha256:" + hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
    )


def test_digest_legacy_uses_default_separators() -> None:
    payload = {"b": 1, "a": "x y"}
    expected = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert hashing.digest_legacy(payload) == expected


def test_digest_and_digest_legacy_differ_for_the_same_payload() -> None:
    """The whole point of keeping two forms — compact vs. spaced JSON hashes
    differently for any payload with more than one key."""
    payload = {"a": 1, "b": 2}
    assert hashing.digest(payload) != hashing.digest_legacy(payload)


def test_digest_text_hashes_bytes_verbatim_no_json_envelope() -> None:
    text = "hello world"
    assert hashing.digest_text(text) == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_normalize_adds_prefix_to_unprefixed() -> None:
    bare = hashlib.sha256(b"x").hexdigest()
    assert hashing.normalize(bare) == f"sha256:{bare}"


def test_normalize_is_idempotent_on_prefixed() -> None:
    prefixed = "sha256:" + hashlib.sha256(b"x").hexdigest()
    assert hashing.normalize(prefixed) == prefixed


def test_normalize_passes_through_none() -> None:
    assert hashing.normalize(None) is None


# --- golden values: byte-identical output across the refactor ------------------


def test_repmap_section_hash_matches_pre_refactor_formula() -> None:
    md = "## Summary\n\nSome narrative text.\n"
    expected = "sha256:" + hashlib.sha256(md.strip().encode()).hexdigest()
    assert section_hash(md) == expected


# --- a still-live v1 shape (grison/state.py keeps needing it — see module docstring
# in grison/model/finding.py) ----------------------------------------------------


def test_evidence_gw_ref_accepts_a_legacy_unprefixed_meta_value() -> None:
    """An old .grison/state entry stored an un-prefixed meta hash; the model must
    still load it (comparison-site normalization happens where it's compared, see
    grison.remote.sync)."""
    ref = EvidenceGwRef(id=1, meta=hashlib.sha256(b"x").hexdigest())
    assert not ref.meta.startswith("sha256:")


def test_evidence_item_roundtrips_with_a_gw_ref() -> None:
    item = EvidenceItem(file="evidence/x.png", gw=EvidenceGwRef(id=1))
    assert item.gw is not None and item.gw.id == 1
