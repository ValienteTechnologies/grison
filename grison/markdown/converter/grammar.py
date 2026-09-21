"""The closed HTML/legacy-template vocabulary the converter accepts, both
directions: tag whitelists, kept attributes, and the shared regexes/constants
that recognize grison's special forms (evidence embeds/cross-references, the
legacy Ghostwriter dot-syntax, and the D10 jinja-escape/raw-block machinery).
See :mod:`grison.markdown.converter`'s module docstring for the full grammar.
"""

from __future__ import annotations

import re

_BLOCK_TAGS = {"p", "ul", "ol", "li", "div"}
_INLINE_TAGS = {"strong", "code", "em", "a", "br"}
_UNWRAP_TAGS = {"span"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_ALLOWED_TAGS = _BLOCK_TAGS | _INLINE_TAGS | _UNWRAP_TAGS
# Heading support is opt-in via ``headings=True`` (never loosened by default) — but
# it is NOT narrative-only: a real Ghostwriter 7.2.6 TipTap editor emits h1-h6 in a
# plain FINDING field too (confirmed against a live export,
# ``tests/fixtures/lab-samples/gw-findings.json``'s library finding 3, whose stored
# ``description`` is ``<h3>Overview</h3><p>...</p>`` — grison's finding adapters call
# both converter functions with ``headings=True`` for exactly this reason). Every
# tag the 7.2.6 editor is confirmed to emit (across the whole ``lab-samples/``
# corpus — findings and wiki pages alike) is already on this allow-list or
# ``_HEADING_TAGS`` below; no other block tag was found.

# Ghostwriter's legacy dot-syntax: r"\{\{\s*\.([^\{\}]*?)\s*\}\}" verbatim from
# html_rich_text.py — whitespace after "{{" and before "}}" is tolerated; the
# captured group is stripped again below, matching Ghostwriter's own
# `.strip()` on `match.group(1)`.
_DOT_FORM_RE = re.compile(r"\{\{\s*\.([^{}]*?)\s*\}\}")
# D10's own escape form (see module docstring and _jinja_escape_html): grison's
# ONE reserved way of spelling a literal template-delimiter token in the pushed
# HTML — recognized here to unwrap it back to that literal token on pull.
_JINJA_STRLIT_RE = re.compile(r"\{\{\s*'(\{\{|\}\}|\{%|%\}|\{#|#\})'\s*\}\}")
# One combined scan for inline "special" brace forms, tried in order: (1)
# grison's own D10 escape form above — unwraps to the literal token it stands
# for, as plain literal text; (2) the narrower legacy dot-form shape (a SUBSET
# of the generic brace shape); (3) any other {{ }}/{% %}/{# #} run, an ACTIVE
# Jinja expression. DOTALL so a multi-line field's expression can span lines.
_INLINE_SPECIAL_RE = re.compile(
    r"\{\{\s*'(?P<strlit>\{\{|\}\}|\{%|%\}|\{#|#\})'\s*\}\}"
    r"|\{\{\s*\.(?P<dot>[^{}]*?)\s*\}\}"
    r"|\{\{(?P<active_expr>.*?)\}\}"
    r"|\{%(?P<active_stmt>.*?)%\}"
    r"|\{#(?P<active_comment>.*?)#\}",
    re.DOTALL,
)
# A literal Jinja ``{% raw %}``/``{% endraw %}`` DELIMITER TOKEN found in STORED
# html — never emitted by grison's own push any more (see ``_jinja_escape_html``'s
# per-token design), but real data can still carry one (an older grison push, or a
# human typing raw-block syntax directly in Ghostwriter's editor): Jinja's own
# semantics say everything between the two is literal, unconditionally. Matched as
# two SEPARATE tokens, never a single paired regex: pairing them requires seeing
# both in the same scan, and the two can legitimately sit in different HTML text
# nodes (an inline tag — ``<strong>``, ``<em>``, ``<a>``, ``<code>`` — between them)
# or in different top-level blocks entirely (module docstring, "Active template
# expressions") — see ``_RawScanState``/``_split_raw_regions`` below, the ONE place
# that pairs them, by scanning every leaf of a document in order against one
# shared, mutable flag rather than re-deriving pairing per leaf.
_RAW_OPEN_RE = r"\{%-?\s*raw\s*-?%\}"
_RAW_CLOSE_RE = r"\{%-?\s*endraw\s*-?%\}"
_RAW_TOKEN_RE = re.compile(rf"(?P<raw_open>{_RAW_OPEN_RE})|(?P<raw_close>{_RAW_CLOSE_RE})")
_UNRESOLVED_RE = re.compile(r"^gw:evidence-ref:(?:id=(?P<id>\d+)|name=(?P<name>.*))$", re.DOTALL)
# The one level of list nesting markdown's own indent-based sub-list syntax can
# express (module docstring, "Nesting"). Shared by both directions' own,
# genuinely different counting: from_html/blocks.py's ``_flatten_nested_list``
# counts ``depth`` from 1 at the first nested level and COLLAPSES (with
# on_loss) anything deeper, since real GW HTML can already have it; to_html/
# blocks.py's ``_render_md_list_node``/``_render_list_item_node`` count
# ``nesting`` from 0 at the top level and hard-REJECT authoring a level this
# deep, since freshly-authored markdown must never grow a shape that can't
# round-trip. Both checks are phrased directly against this one constant so
# the relationship — one shared limit, two different responses — stays
# explicit rather than being two independently-tuned magic numbers.
_MAX_NESTED_LIST_DEPTH = 1
_GW_REF_ENCODED_ATTR = "data-gw-ref-encoded"
_EVIDENCE_DIV_CLASS = "richtext-evidence"
_EVIDENCE_ID_ATTR = "data-evidence-id"

# Per tag, the attributes that are load-bearing or captured specifically for
# on_loss reporting (F4/F6 — the span highlight/link rel-target canonicalization
# messages need the actual values). Everything else present on ANY allowed tag is
# captured generically (see _TreeBuilder._open) and reported via on_loss at render
# time — no tag silently drops an attribute (the survey that found "attributes on
# other allowed tags... dropped without on_loss" is what this closes).
_KEEP_ATTRS: dict[str, tuple[str, ...]] = {
    "a": ("href", "rel", "target", "title"),
    "span": ("data-color", "style", _GW_REF_ENCODED_ATTR, "data-gw-ref"),
    "ol": ("start", "type"),
    "div": ("class", _EVIDENCE_ID_ATTR),
}
