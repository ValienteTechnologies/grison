"""Proofs for grison/hashing.py — the one canonicalization the engine's merge-base
hashes, the validator's mirror digests, and scaffold's idempotency checks all share.

Workspace format v1's three predating call sites (``gwmap.content_hash``,
``bsmap.bs_content_hash``, ``repmap.section_hash``) and their golden byte-compat
tests are gone along with format v1 — see grison/hashing.py's module docstring.
"""

from __future__ import annotations

import hashlib
import json

from grison import hashing


def test_digest_uses_compact_separators() -> None:
    assert hashing.digest({"b": 1, "a": 2}) == (
        "sha256:" + hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
    )


def test_digest_text_hashes_bytes_verbatim_no_json_envelope() -> None:
    text = "hello world"
    assert hashing.digest_text(text) == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_digest_sorts_keys_regardless_of_input_order() -> None:
    assert hashing.digest({"z": 1, "a": 2}) == hashing.digest({"a": 2, "z": 1})


def test_digest_rejects_non_ascii_by_keeping_it_literal_not_escaped() -> None:
    # ensure_ascii=False: a non-ASCII payload hashes its literal UTF-8 bytes, not a
    # \uXXXX-escaped form — this is what makes digest byte-identical across a
    # workspace regardless of the platform's default json.dumps ensure_ascii setting.
    payload = {"title": "café"}
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    assert hashing.digest(payload) == expected
