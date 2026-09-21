"""D10 (active Jinja template expressions) and the ``{% raw %}...{% endraw %}``
scan both directions need — see :mod:`grison.markdown.converter`'s module
docstring, "Active template expressions", for the full design rationale.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from grison.markdown.converter.grammar import _JINJA_STRLIT_RE, _RAW_TOKEN_RE
from grison.markdown.converter.nodes import _report_loss


@dataclass
class _RawScanState:
    """Shared, mutable "are we currently inside a real, unescaped Jinja
    ``{% raw %}...{% endraw %}`` region" flag, threaded through one whole
    :func:`html_to_md` call — the ONE rule that replaces the old per-text-run
    ``_RAW_BLOCK_RE`` pair match (which only ever saw one HTML text node at a
    time, so an opener and its closer split across an inline tag, e.g.
    ``{% raw %}<strong>{{ x }}</strong>{% endraw %}``, or across two separate
    top-level blocks, e.g. one ``<p>`` ending mid-region and the next ``<p>``
    carrying the closer, were each invisible to it — see module docstring). Every
    leaf of text in the document, in document order, is scanned against this one
    flag (:func:`_split_raw_regions`), so a region is recognised wherever its two
    tokens actually fall, never re-derived per leaf. ``_open_reported`` tracks
    whether the region open right now already fired its "still open at a block
    boundary" ``on_loss`` message once (:func:`_note_block_boundary`), so a region
    spanning three or more blocks is reported once, not once per boundary."""

    in_raw: bool = False
    _open_reported: bool = False


def _split_raw_regions(
    text: str, state: _RawScanState, on_loss: Callable[[str], None] | None
) -> list[tuple[bool, str]]:
    """Split one leaf of text into ``(is_literal, piece)`` runs against
    ``state``'s shared flag, dropping every ``{% raw %}``/``{% endraw %}`` marker
    token itself — never left in the output (module docstring). A closer flips
    the flag back off and fires the "resolved to literal text" ``on_loss``
    message exactly once per region, regardless of how many leaves/tags/blocks it
    actually spanned. An opener with no closer anywhere in the rest of the
    document leaves every later piece — in this leaf and every leaf after it,
    across every remaining tag and block — literal for good (Jinja's own raw
    block, once opened, disables evaluation unconditionally until its closer;
    with none, the whole field is already un-exportable regardless of what
    grison renders here)."""
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _RAW_TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append((state.in_raw, text[pos : m.start()]))
        if m.group("raw_open") and not state.in_raw:
            state.in_raw = True
            state._open_reported = False
        elif m.group("raw_close") and state.in_raw:
            state.in_raw = False
            state._open_reported = False
            _report_loss(
                on_loss,
                "{% raw %}...{% endraw %} block resolved to its literal text (normalization)",
            )
        pos = m.end()
    if pos < len(text):
        out.append((state.in_raw, text[pos:]))
    return out


def _note_block_boundary(state: _RawScanState, on_loss: Callable[[str], None] | None) -> None:
    """Called between top-level blocks (:func:`_render_top_level_blocks`): a
    region still open at a block boundary is a raw region spanning two block
    elements — reported once via ``on_loss`` (the flag itself already makes both
    halves render literally; this call only adds visibility), never re-reported
    for the same still-open region at the next boundary."""
    if state.in_raw and not state._open_reported:
        state._open_reported = True
        _report_loss(
            on_loss,
            "{% raw %} block still open at the end of a block, continuing into the next "
            "block element (normalization: rendered literally in both halves, never as "
            "an active expression)",
        )


def _unwrap_jinja_escapes(text: str, raw_state: _RawScanState) -> str:
    """Resolve every D10 escape token (``_jinja_escape_html``'s per-token form) AND
    every ``{% raw %}...{% endraw %}`` region back to plain literal text — used for
    ``<code>`` content, which (per module docstring) is always literal regardless of
    shape, so a raw-wrap found there is exactly as redundant as it is in plain text
    and resolves the same way (see ``_render_text_run``, same ``raw_state`` shared
    across the whole document so a region's opener/closer pairing is unaffected by
    whether either half happens to sit inside a ``<code>`` element); any D10 strlit
    token found INSIDE a raw region (only possible in already-malformed legacy
    data, since grison's own push never nests them) is unwrapped too, defensively.
    Never reports via ``on_loss`` — code content never has, and gains no new
    reporting here (:func:`_render_code_text` has no ``on_loss`` of its own)."""
    out: list[str] = []
    for _is_literal, piece in _split_raw_regions(text, raw_state, None):
        out.append(_JINJA_STRLIT_RE.sub(lambda m: m.group(1), piece))
    return "".join(out)


# Every individual two-character sequence that can START a Jinja lexer token —
# matched and neutralized ONE TOKEN AT A TIME, deliberately not as matched
# {{...}}/{%...%}/{#...#} PAIRS (an earlier design did that, wrapping the whole
# matched span in Jinja's own {% raw %}...{% endraw %} block tag — abandoned:
# a LONE, never-closed opener, e.g. author text containing just "{%" with no
# "%}" anywhere else in the field, is just as fatal to a real export as a
# matched pair read differently than intended — Jinja's compiler aborts the
# WHOLE report on an unterminated tag, and {% raw %} itself has no representation
# for "start a raw block that's never closed" either, so pairing can't fix this
# class of bug at all). Per-token substitution sidesteps pairing entirely: every
# occurrence — lone, matched, nested/overlapping ("{{ {% }}"), or the literal
# text "{% raw %}"/"{% endraw %}" typed by an author with no jinja intent at
# all — is neutralized uniformly, with no special-casing of any shape.
_JINJA_TOKEN_RE = re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}")


def _jinja_escape_html(escaped_text: str) -> str:
    """D10: replace every occurrence of a literal Jinja delimiter token in
    ``escaped_text`` (already HTML-escaped, so this only ever sees literal
    ``{``/``}``/``%``/``#`` — HTML escaping doesn't touch any of them) with a
    Jinja string-literal expression that evaluates back to that exact token,
    e.g. ``{%`` -> ``{{ '{%' }}`` — verified against a real Ghostwriter 7.2.6
    export to survive template compilation byte-for-byte, including a lone
    unclosed opener and text containing the literal strings "{% raw %}"/
    "{% endraw %}" (see module docstring and
    the rework lab proof (``proofs/d10-jinja-escape-lab.md``, kept outside this repo)). Applied
    identically whether the fragment sits in plain paragraph text or inside a
    ``<code>`` element — Jinja's lexer scans the whole HTML string as one
    template regardless of what tag a substring sits inside, so an inserted
    ``{{ '...' }}`` expression evaluates the same way in either position.
    ``_JINJA_STRLIT_RE`` above is the exact inverse, used to unwrap this form
    back to its literal token on pull."""
    return _JINJA_TOKEN_RE.sub(lambda m: "{{ '" + m.group(0) + "' }}", escaped_text)
