"""markdown->html: evidence embeds (D1/D9) and cross-references (D1) — pushing
markdown's image/link forms (or a reserved ``gw:evidence-ref:...`` marker) into
Ghostwriter's native HTML.
"""

from __future__ import annotations

import re

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.grammar import (
    _EVIDENCE_DIV_CLASS,
    _EVIDENCE_ID_ATTR,
    _GW_REF_ENCODED_ATTR,
)
from grison.markdown.converter.mdtext import _esc
from grison.markdown.converter.refs_codec import _encode_gw_ref
from grison.markdown.converter.refs_render import _no_resolver_error
from grison.markdown.refs import RefResolver


def _render_unresolved_marker(m: re.Match[str], *, position: str) -> str:
    """Reconstruct the exact canonical Ghostwriter construct a
    ``gw:evidence-ref:...`` marker names, bypassing ``refs`` entirely — the
    remote identity is already fully known from the marker itself (see module
    docstring, "Unresolved references")."""
    if m.group("id") is not None:
        if position != "block":
            raise ConverterError(
                "gw:evidence-ref:id=... is only valid as its own paragraph/list-item "
                "block (an evidence div is never an inline construct)"
            )
        return f'<div class="{_EVIDENCE_DIV_CLASS}" {_EVIDENCE_ID_ATTR}="{m.group("id")}"></div>'
    name = m.group("name")
    if position == "block":
        return f"<p>{{{{.{_esc(name)}}}}}</p>"
    return f'<span {_GW_REF_ENCODED_ATTR}="{_encode_gw_ref(name)}"></span>'


def _push_embed_ref(src: str, refs: RefResolver | None) -> str:
    if refs is None:
        raise _no_resolver_error(
            "image/evidence embed found", func="md_to_html", remedy="remove the image"
        )
    remote = refs.to_remote(src)
    if remote is None or remote.id is None:
        raise ConverterError(f"evidence path does not resolve via refs: {src!r}")
    return f'<div class="{_EVIDENCE_DIV_CLASS}" {_EVIDENCE_ID_ATTR}="{remote.id}"></div>'


def _push_cross_ref(href: str, refs: RefResolver | None) -> str:
    if refs is None:
        raise _no_resolver_error(
            "cross-reference to an evidence path found",
            func="md_to_html",
            remedy="use a non-evidence link",
        )
    remote = refs.to_remote(href)
    if remote is None or remote.name is None:
        raise ConverterError(f"evidence path does not resolve via refs: {href!r}")
    return f'<span {_GW_REF_ENCODED_ATTR}="{_encode_gw_ref(remote.name)}"></span>'
