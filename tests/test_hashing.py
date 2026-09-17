"""Proofs for grison/hashing.py.

The golden-value tests below reconstruct the pre-refactor formula independently
(inline ``json.dumps``/``hashlib``, never calling into ``grison.hashing`` itself) and
assert it against the real call sites' live output — the proof that routing
``gwmap.content_hash``/``bsmap.bs_content_hash``/``repmap.section_hash`` through
``grison.hashing`` did not change a single persisted byte.
"""

from __future__ import annotations

import hashlib
import json

from grison import hashing
from grison.model.finding import Cvss, EvidenceGwRef, EvidenceItem, Finding, GrisonMeta, GwRef
from grison.remote.bsmap import MethPage, bs_content_hash
from grison.remote.gwmap import content_hash, evidence_meta_hash
from grison.remote.repmap import section_hash


def _legacy_dict_hash(payload: object) -> str:
    """Independent reimplementation of the pre-refactor dict-payload formula —
    default (non-compact) JSON separators, prefixed."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def test_gwmap_content_hash_matches_pre_refactor_formula() -> None:
    """content_hash's payload shape (see gwmap._syncable_view) reproduced inline,
    independent of grison.hashing, and pinned before the refactor: routing it
    through hashing.digest_legacy must not change one byte of a value already
    persisted as thousands of findings' merge base."""
    f = Finding(
        grison=GrisonMeta(tier="library", gw=GwRef(table="finding", id=1)),
        severity="critical",
        finding_type="network",
        cvss=Cvss(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"),
        cwe=["CWE-79"],
        tags=["auth"],
        title="Reflected XSS",
        description="desc",
        impact="impact",
        mitigation="mitigation",
        replication_steps="steps",
        references="refs",
    )
    expected_payload = {
        "title": f.title,
        "severity": f.severity.value,
        "finding_type": f.finding_type.value,
        "cvss_vector": f.cvss.vector,
        "cwe": sorted(f.cwe),
        "tags": sorted(f.tags),
        "affected_entities": f.affected_entities,
        "description": f.description,
        "impact": f.impact,
        "mitigation": f.mitigation,
        "replication_steps": f.replication_steps,
        "references": f.references,
        "evidence": [],
    }
    assert content_hash(f) == _legacy_dict_hash(expected_payload)


def test_bsmap_bs_content_hash_matches_pre_refactor_formula() -> None:
    page = MethPage(
        page_id=5, book_id=1, book="methodology", title="Recon",
        body="# Recon\n\nsteps", chapter="network", priority=3,
        tags=[{"name": "phase", "value": "1"}],
    )
    expected_payload = {
        "title": page.title,
        "book": page.book,
        "chapter": page.chapter or "",
        "priority": page.priority,
        "tags": page.tags,
        "body": page.body,
    }
    assert bs_content_hash(page) == _legacy_dict_hash(expected_payload)


def test_repmap_section_hash_matches_pre_refactor_formula() -> None:
    md = "## Summary\n\nSome narrative text.\n"
    expected = "sha256:" + hashlib.sha256(md.strip().encode()).hexdigest()
    assert section_hash(md) == expected


# --- the two newly-prefixed hashes ----------------------------------------------


def test_evidence_meta_hash_is_now_prefixed() -> None:
    h = evidence_meta_hash("cap", "friendly", "desc")
    assert h.startswith("sha256:")
    # the hex portion still matches the pre-refactor (un-prefixed) formula exactly —
    # only the prefix is new, so an old stored value's hex digest still compares
    # equal via hashing.normalize()
    legacy = hashlib.sha256(
        json.dumps(
            {"caption": "cap", "friendly_name": "friendly", "description": "desc"},
            sort_keys=True, ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert h == f"sha256:{legacy}"


def test_evidence_gw_ref_accepts_a_legacy_unprefixed_meta_value() -> None:
    """An old .grison/state entry stored an un-prefixed meta hash; the model must
    still load it (comparison-site normalization happens where it's compared, see
    grison.remote.sync)."""
    ref = EvidenceGwRef(id=1, meta=hashlib.sha256(b"x").hexdigest())
    assert not ref.meta.startswith("sha256:")


def test_evidence_item_roundtrips_with_a_gw_ref() -> None:
    item = EvidenceItem(file="evidence/x.png", gw=EvidenceGwRef(id=1))
    assert item.gw is not None and item.gw.id == 1
