"""HTML->markdown: inline-content rendering (``<strong>``/``<em>``/``<code>``/
``<a>``/``<br>``/cross-reference ``<span>``), the raw-text-run scan for D10's
active/escaped Jinja forms, and ``<code>`` content flattening.
"""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.from_html.evidence import (
    _render_cross_ref_span,
    _render_dot_form_inline,
)
from grison.markdown.converter.grammar import _INLINE_SPECIAL_RE
from grison.markdown.converter.inline_normalize import _merge_adjacent_inline
from grison.markdown.converter.jinja import (
    _RawScanState,
    _split_raw_regions,
    _unwrap_jinja_escapes,
    _unwrap_jinja_strlit,
)
from grison.markdown.converter.mdtext import (
    _fence_code,
    _md_escape_quotes,
    _md_escape_run,
    _wrap_delim,
)
from grison.markdown.converter.nodes import _Node, _report_dropped_attrs, _report_loss
from grison.markdown.refs import RefResolver


def _render_inline(
    nodes: list[_Node | str],
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    parts = []
    for n in _merge_adjacent_inline(nodes, on_loss):
        if isinstance(n, str):
            parts.append(_render_text_run(n, refs, on_loss, raw_state))
        elif n.tag == "br":
            parts.append("\n")
        elif n.tag == "strong":
            _report_dropped_attrs(n, on_loss)
            inner = _render_inline(n.children, refs, on_loss, raw_state)
            parts.append(_wrap_delim("**", inner, "strong", on_loss))
        elif n.tag == "em":
            _report_dropped_attrs(n, on_loss)
            inner = _render_inline(n.children, refs, on_loss, raw_state)
            parts.append(_wrap_delim("*", inner, "em", on_loss))
        elif n.tag == "code":
            _report_dropped_attrs(n, on_loss)
            parts.append(_fence_code(_render_code_text(n.children, raw_state)))
        elif n.tag == "a":
            href = n.attrs.get("href", "")
            rel = n.attrs.get("rel")
            if rel is not None and rel.strip() != "noopener":
                _report_loss(on_loss, f'link rel={rel!r} canonicalized to "noopener" on push')
            target = n.attrs.get("target")
            if target is not None and target.strip() != "_blank":
                _report_loss(on_loss, f'link target={target!r} canonicalized to "_blank" on push')
            _report_dropped_attrs(n, on_loss)
            title = n.attrs.get("title")
            title_part = f' "{_md_escape_quotes(title)}"' if title else ""
            inner_a = _render_inline(n.children, refs, on_loss, raw_state)
            parts.append(f"[{inner_a}]({href}{title_part})")
        elif n.tag == "span":
            # A plain (non cross-reference) <span> is always spliced away by
            # _flatten_transparent_spans() inside _merge_adjacent_inline()
            # above, before this dispatch ever sees it — so the only <span>
            # that can reach here is the opaque cross-reference marker.
            parts.append(_render_cross_ref_span(n, refs, on_loss))
        else:
            raise ConverterError(f"unsupported tag in inline content: <{n.tag}>")
    return "".join(parts)


def _render_text_run(
    text: str,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    raw_state: _RawScanState,
) -> str:
    """Render one literal HTML text node as markdown: every piece of ``text``
    that :func:`_split_raw_regions` marks as inside a ``{% raw %}...{% endraw %}``
    region (see ``raw_state``) resolves to its own plain literal text FIRST and
    unconditionally — Jinja's raw block already means "never evaluate this", so
    nothing inside one is ever treated as an active expression or re-escaped as a
    ``gw:`` marker, matching D10's contract that the escaping mechanism is never
    nested; every piece outside a raw region then goes through the ordinary
    special-form scan: a D10 escape token (see ``_jinja_escape_html``) unwraps to
    the literal delimiter text it stands for first, then legacy ``.ref``
    cross-refs, then active (un-escaped) Jinja delimiters (see module docstring);
    anything else is escaped literal text."""
    out: list[str] = []
    for is_literal, piece in _split_raw_regions(text, raw_state, on_loss):
        if is_literal:
            literal = _unwrap_jinja_strlit(piece)
            out.append(_md_escape_run(literal))
        else:
            out.append(_render_text_run_specials(piece, refs, on_loss))
    return "".join(out)


def _render_text_run_specials(
    text: str, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    """The non-raw-block half of :func:`_render_text_run`'s scan — factored out so
    a raw region can bypass it entirely instead of being scanned for (and
    misinterpreted as) an active expression."""
    out: list[str] = []
    pos = 0
    for m in _INLINE_SPECIAL_RE.finditer(text):
        if m.start() > pos:
            out.append(_md_escape_run(text[pos : m.start()]))
        if m.group("strlit") is not None:
            out.append(_md_escape_run(m.group("strlit")))
        elif m.group("dot") is not None:
            out.append(_render_dot_form_inline(m.group("dot"), refs, on_loss))
        else:
            out.append(_fence_code(f"gw:{m.group(0)}"))
        pos = m.end()
    out.append(_md_escape_run(text[pos:]))
    return "".join(out)


def _render_code_text(nodes: list[_Node | str], raw_state: _RawScanState) -> str:
    """<code> content is never inline-parsed, so just flatten its text (unwrapping
    any cosmetic <span>, and unwrapping each D10 escape token — see
    ``_jinja_escape_html`` — back to the literal delimiter text it stands for —
    but rejecting any other nested tag). Content inside <code> is always
    literal: an active (un-escaped) template expression here is not
    distinguished from literal braces (see module docstring)."""
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(_unwrap_jinja_escapes(n, raw_state))
        elif n.tag == "span":
            parts.append(_render_code_text(n.children, raw_state))
        else:
            raise ConverterError(f"unsupported nested tag inside <code>: <{n.tag}>")
    return "".join(parts)
