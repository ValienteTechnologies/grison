"""Ghostwriter's own cross-reference attribute encoding, plus the deterministic
cross-reference link-text derivation both directions share.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from grison.markdown.refs import LocalRef


def _encode_gw_ref(name: str) -> str:
    """Ghostwriter's cross-reference attribute encoding: each character's Unicode
    code point as lowercase hex, hyphen-joined (``encodeReference`` in
    ``javascript/src/tiptap_gw/jinja_literal.ts``). Python iterates a ``str`` by
    code point already, so this is a direct port."""
    return "-".join(format(ord(ch), "x") for ch in name)


def _decode_gw_ref(encoded: str) -> str | None:
    """Inverse of :func:`_encode_gw_ref` (``decodeReference`` in the same file):
    ``None`` on anything that isn't a valid hex-hyphen sequence or decodes to an
    invalid code point."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]+(?:-[0-9a-fA-F]+)*)?", encoded):
        return None
    try:
        return "".join(chr(int(part, 16)) for part in encoded.split("-") if part)
    except ValueError:
        return None


def _ref_display_text(local: LocalRef) -> str:
    """Deterministic cross-reference link text (the native span has no text of its
    own to preserve — see module docstring): the evidence's caption, else the
    path's filename stem."""
    return local.caption or PurePosixPath(local.path).stem
