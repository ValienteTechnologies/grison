"""HTML->markdown: evidence embeds (D1/D9) and cross-references (D1), both the
native Ghostwriter HTML forms and the legacy dot-syntax.
"""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.grammar import (
    _DOT_FORM_RE,
    _EVIDENCE_DIV_CLASS,
    _EVIDENCE_ID_ATTR,
    _GW_REF_ENCODED_ATTR,
)
from grison.markdown.converter.nodes import _Node
from grison.markdown.converter.refs_codec import _decode_gw_ref
from grison.markdown.converter.refs_render import (
    _cross_ref_link_md,
    _no_resolver_error,
    _unresolved_marker_md,
)
from grison.markdown.refs import RefResolver, RemoteRef


def _try_render_dot_embed(
    text: str, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str | None:
    """If ``text`` (a block's ENTIRE, sole content) is exactly a bare legacy
    dot-form (``{{.Name}}`` — not ``ref``/``caption`` prefixed), render it as an
    image embed line (or an unresolved-reference placeholder). Returns ``None`` if
    ``text`` isn't of that shape, so the caller falls through to normal rendering."""
    m = _DOT_FORM_RE.fullmatch(text.strip())
    if not m:
        return None
    contents = m.group(1).strip()
    if contents.startswith("ref ") or contents == "caption" or contents.startswith("caption "):
        return None  # not a bare embed form — handled inline / rejected there
    return _render_resolved_embed(RemoteRef("gw-evidence", None, contents, None), refs, on_loss)


def _render_resolved_embed(
    remote: RemoteRef, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    if refs is None:
        raise _no_resolver_error(
            "evidence reference found", func="html_to_md", remedy="drop the reference"
        )
    local = refs.to_local(remote)
    if local is None:
        marker = f"id={remote.id}" if remote.id is not None else f"name={remote.name}"
        return _unresolved_marker_md(marker, on_loss)
    title = f' "{local.description}"' if local.description else ""
    return f"![{local.caption}]({local.path}{title})"


def _render_evidence_div(
    node: _Node, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    classes = (node.attrs.get("class") or "").split()
    if _EVIDENCE_DIV_CLASS not in classes or _EVIDENCE_ID_ATTR not in node.attrs or node.children:
        raise ConverterError(
            f'unsupported <div> (only <div class="{_EVIDENCE_DIV_CLASS}" '
            f'{_EVIDENCE_ID_ATTR}="N"></div> is allowed)'
        )
    raw_id = node.attrs[_EVIDENCE_ID_ATTR]
    try:
        ev_id = int(raw_id)
    except ValueError as e:
        raise ConverterError(f"invalid {_EVIDENCE_ID_ATTR}: {raw_id!r}") from e
    return _render_resolved_embed(RemoteRef("gw-evidence", ev_id, None, None), refs, on_loss)


def _render_cross_ref_span(
    n: _Node, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    if n.children:
        raise ConverterError("unsupported <span data-gw-ref…>: expected no content")
    encoded = n.attrs.get(_GW_REF_ENCODED_ATTR)
    if encoded is not None:
        name = _decode_gw_ref(encoded)
        if name is None:
            raise ConverterError(f"invalid {_GW_REF_ENCODED_ATTR}: {encoded!r}")
    else:
        name = n.attrs["data-gw-ref"]
    if refs is None:
        raise _no_resolver_error(
            "cross-reference found", func="html_to_md", remedy="drop the reference"
        )
    local = refs.to_local(RemoteRef("gw-evidence", None, name, None))
    if local is None:
        return _unresolved_marker_md(f"name={name}", on_loss)
    return _cross_ref_link_md(local)


def _render_dot_form_inline(
    contents: str, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    contents = contents.strip()
    if contents.startswith("ref "):
        name = contents[4:].strip()
        if refs is None:
            raise _no_resolver_error(
                "legacy {{.ref}} reference found", func="html_to_md", remedy="drop the reference"
            )
        local = refs.to_local(RemoteRef("gw-evidence", None, name, None))
        if local is None:
            return _unresolved_marker_md(f"name={name}", on_loss)
        return _cross_ref_link_md(local)
    if contents == "caption" or contents.startswith("caption "):
        raise ConverterError(
            f"unsupported legacy construct {{{{.{contents}}}}}: grison has no "
            "markdown form for a standalone Ghostwriter caption (only for an "
            "evidence embed or cross-reference, D1) — this record can't be pulled "
            "as-is; open it in Ghostwriter's rich-text editor and replace this "
            "{{.caption...}} tag with an actual embedded image or plain text, "
            "then re-sync"
        )
    raise ConverterError(
        f"unsupported legacy construct {{{{.{contents}}}}}: a bare evidence "
        "reference must be its own paragraph to become an image embed"
    )
