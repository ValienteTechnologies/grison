"""HTML<->markdown converter for the tiny closed vocabulary Ghostwriter's rich-text
fields accept, plus the three special reference/template forms grison layers on top.

Ghostwriter finding fields render a small, fixed subset of HTML (paragraphs,
lists, bold/code/em/links/hard-breaks, plus TinyMCE's cosmetic ``<span>`` highlight
wrapper). grison round-trips those fields against local markdown: anything outside
the whitelist below must fail loudly (:class:`ConverterError`, naming the construct)
rather than degrade silently or get dropped on the floor.

Whitelist (both directions):
  block:  ``<p>`` <-> paragraph, ``<ul><li>`` <-> ``- `` list item,
          ``<ol><li>`` <-> ``1. `` list item (numbered sequentially on emit;
          ``<ol start="N">`` <-> the first item's literal number)
  inline: ``<strong>`` <-> ``**bold**``/``__bold__``, ``<code>`` <-> `` `code` ``
          (a longer backtick fence is used when the code text itself contains a
          run of backticks), ``<em>`` <-> ``*em*``/``_em_``,
          ``<strong><em>`` <-> ``***both***``,
          ``<a href[ title]>`` <-> ``[text](url[ "title"])`` (a literal ``)`` in the
          URL and backslash-escaped punctuation in text both round-trip),
          ``<br>`` <-> a hard line break inside a paragraph (every markdown newline
          in a paragraph — bare, or CommonMark's own ``\\`` / trailing-two-spaces
          hard-break syntax — is treated as one; grison has no soft-break concept).
  ``<span>`` is unwrapped (kept, tag dropped) rather than rejected, since TinyMCE
  wraps highlighted text in it. An ``<ol type="...">`` (a/i/A/I numbering style) is
  dropped the same way — markdown can't represent it — and reported via
  ``on_loss``.

Nesting: inline tokens nest arbitrarily inside bold/em/link text in both
directions. Lists support one level of nesting: a ``<ul>``/``<ol>`` nested inside
an ``<li>`` renders as a 2-space-indented sub-item (``  - `` or ``  N. `` per its
own tag); a list nested inside ONE OF THOSE (three or more levels deep in the
source) collapses into that same single sub-level. ``<ul>`` and ``<ol>`` mix
freely at any level. A list item containing more than one block — GW's real
``<li><p>…</p><p>…</p></li>`` "loose" shape, or a paragraph followed by an evidence
embed (see below) — renders as a blank-line-separated, indented continuation of
the SAME item (CommonMark's own loose-list-item syntax):

    - first paragraph

      second paragraph
    - next item

An item with exactly one paragraph (with or without a nested list) still renders
bare, with no ``<p>`` wrapping, as before. This multi-block support is one level
only — a nested (2nd-level) list item with multiple blocks of its own is outside
this vocabulary.

Special forms (on top of the plain-prose whitelist above), all needing a
:class:`~grison.markdown.refs.RefResolver` passed as ``refs=``; without one, any
of them raises ``ConverterError`` naming what to do instead (pass ``refs=``):

  Embed (D1/D9): an image whose paragraph contains nothing else —
  ``![caption](evidence/file.png "optional description")`` — as a top-level block
  or as its own block inside a list item. On push this becomes Ghostwriter's
  native node ``<div class="richtext-evidence" data-evidence-id="N"></div>``
  (matches the TipTap ``evidence`` node's ``parseHTML``/``renderHTML`` — see
  ``javascript/src/tiptap_gw/evidence.tsx`` in the Ghostwriter source, tag
  v7.2.6). On pull, both that div and the legacy raw text ``{{.FriendlyName}}``
  (its own paragraph; any whitespace around the dot-form's contents is tolerated,
  matching Ghostwriter's own
  ``r"\\{\\{\\s*\\.([^\\{\\}]*?)\\s*\\}\\}"`` regex in
  ``html_rich_text.py``) become the same image line. Neither HTML form carries a
  caption/description — those are the evidence row's own fields, synced
  separately — so ``html_to_md`` gets them from
  :meth:`~grison.markdown.refs.RefResolver.to_local`. A reference the resolver
  can't resolve locally (e.g. not synced down yet) round-trips as an inert,
  visible placeholder instead of failing the document — see "Unresolved
  references" below.

  Cross-reference (D1): a plain link to an evidence path,
  ``[text](evidence/file.png)`` (only a link whose URL starts with ``evidence/``
  is ever treated as one — an ordinary external link is never touched). On push
  this becomes Ghostwriter's native ``<span data-gw-ref-encoded="…"></span>`` —
  an empty, atomic node with NO text content in storage (confirmed against
  ``javascript/src/tiptap_gw/jinja_literal.ts``'s ``JinjaReference`` node: its
  ``renderHTML`` never has a content hole; the "Figure N"-looking text an author
  sees is a live editor node-view, never serialized). The attribute value is
  ``ref`` code-point-hex-encoded, hyphen-joined (``encodeReference``/
  ``decodeReference`` in that file) — grison reuses the referenced evidence's
  ``RemoteRef.name`` as ``ref``. The legacy text form is ``{{.ref name}}`` (same
  dot-syntax regex, dispatched to ``jinja_funcs.ref`` in
  ``ghostwriter/modules/reportwriter/jinja_funcs.py``). Since the native span
  carries no text at all, the markdown link's own text is not load-bearing for
  the HTML round trip; ``html_to_md`` synthesizes it deterministically from the
  resolved evidence's caption (falling back to the path's filename stem), so
  ``html_to_md(md_to_html(x))`` reaches a stable fixed point (matching the
  existing cosmetic-normalization pattern used for e.g. ``_em_`` -> ``*em*``)
  rather than preserving arbitrary author-chosen link text, which the HTML has no
  room for.

  Unresolved references: when :meth:`~grison.markdown.refs.RefResolver.to_local`
  returns ``None`` for a reference recovered from HTML, ``html_to_md`` keeps it —
  visibly and losslessly — as inline code with the reserved ``gw:`` prefix (the
  same namespace used for active template expressions, below) instead of
  reproducing the plain image/link form it can't fill in:
  `` `gw:evidence-ref:id=42` `` for a native div with no local match, or
  `` `gw:evidence-ref:name=SomeName` `` for a legacy dot-form or cross-reference
  with no local match. Each is reported via ``on_loss`` as an unresolved
  reference (chosen over a dedicated callback to keep the interface minimal — the
  message text is what carries the specifics). On push, ``md_to_html`` recognizes
  this reserved form directly (bypassing ``refs`` entirely, since the remote
  identity is already fully known) and re-emits the exact same canonical
  construct: an own-block id marker becomes the native evidence div; an
  own-block name marker (no id available) becomes the legacy ``{{.name}}`` text
  (the only push-able form without an id); an inline name marker becomes the
  native cross-reference span. An inline id marker is invalid (a div is never an
  inline construct) and raises ``ConverterError``.

  Active template expressions (D10): Ghostwriter compiles every rich-text field
  as a Jinja template at export
  (``ghostwriter/modules/reportwriter/base/html_rich_text.py``'s
  ``rich_text_template``). With ``jinja_escape=True`` (the default — set for
  Ghostwriter-bound text; narrative/report fields and finding fields alike),
  ``md_to_html`` neutralizes every literal Jinja delimiter TOKEN — ``{{``,
  ``}}``, ``{%``, ``%}``, ``{#``, ``#}`` — found in plain author text (including
  inside inline code), ONE TOKEN AT A TIME, by replacing it with a Jinja
  string-literal expression that evaluates back to that exact token, e.g.
  ``{%`` becomes ``{{ '{%' }}``, inserted directly as text — no HTML node at
  all. This is deliberately per-token, not pair-based (an earlier design
  matched whole ``{{...}}``/``{%...%}``/``{#...#}`` spans and wrapped each one
  in Jinja's own ``{% raw %}...{% endraw %}`` block tag) — pairing can't
  neutralize a LONE, never-closed opener (author text containing just ``{%``
  with no ``%}`` anywhere else in the field is just as fatal to a real export
  as a matched pair read differently than intended: Jinja's compiler aborts
  the WHOLE report on an unterminated tag), and ``{% raw %}`` has no
  representation for "start a raw block that's never closed" either. Per-token
  substitution sidesteps pairing (and therefore lone openers, nested/
  overlapping delimiters like ``{{ {% }}``, and author text that happens to
  spell out the literal strings ``{% raw %}``/``{% endraw %}`` with no jinja
  intent at all) uniformly, with no special-casing of any shape. Verified
  against a real Ghostwriter 7.2.6 lab server
  (``/home/tfp/repos/grison-rework/proofs/d10-jinja-escape-lab.md``): an
  unescaped ``{{7*7}}`` silently evaluates on export, an unescaped unknown tag
  (e.g. ``{% debug %}``) aborts the WHOLE report export with a 500 (Jinja
  compiles the entire field as one template before rendering — no per-record
  isolation), and the Jinja string-literal escape form survives a real export
  byte-for-byte, including a lone unclosed opener and literal ``{% raw %}``/
  ``{% endraw %}`` author text. An earlier design wrapped literal braces in a
  ``<div data-gw-jinja-literal="true">`` HTML node instead. Ghostwriter's TipTap
  schema DOES recognize that node (a real ``JinjaLiteral`` node,
  ``javascript/src/tiptap_gw/jinja_literal.ts``, ``content: "block+"``) — that
  assumption in an earlier draft of this module was wrong — but it was still
  abandoned: a block-content node needs the same always-``<p>``-wrapped
  handling as a list item (more machinery, no benefit), and plain text needs no
  schema support at all, in ANY editor, by construction — a strictly simpler,
  independently-proven guarantee for the same job. ``html_to_md`` unwraps
  grison's own ``{{ '<token>' }}`` escape form back to the literal token it
  stands for — literal-brace text round-trips as plain text with no special
  markdown form, since D10 escaping regenerates the wrapper on the next push
  purely from the braces being present.

  Conversely, a genuine ACTIVE (un-wrapped) ``{{ }}``/``{% %}``/``{# #}``
  sequence found in pulled HTML — one Ghostwriter's own Jinja compiler will
  execute at export, whether or not that was intentional — must not be silently
  neutralized into inert literal text by D10 escaping. ``html_to_md`` represents
  each such sequence as inline code with the reserved ``gw:`` prefix,
  e.g. `` `gw:{{ client.name }}` ``, keeping the exact original text between the
  delimiters; ``md_to_html`` recognizes this reserved form and emits the
  un-escaped, active expression directly, bypassing D10 escaping for that token.
  An active expression found INSIDE a ``<code>`` element is, by contrast, always
  treated as literal (D10-escaped on push if it contains braces) — nesting a
  code span inside another is not valid markdown, and an intentionally-active
  expression inside an inline code span is rare enough that grison declines to
  invent a three-way split to preserve it; write it outside code if it must stay
  active.

  The legacy evidence forms (``{{.name}}``, ``{{.ref name}}``) are handled above,
  not by the active/literal distinction. Any OTHER dot-form (``{{.caption}}``,
  ``{{.caption name}}``, or a bare ``{{.name}}`` NOT alone in its paragraph) is
  outside grison's vocabulary and raises ``ConverterError`` — naming it and, for
  ``{{.caption...}}`` specifically (the same class as a table: an unsupported
  construct blocking that one record's pull), telling the author what to do
  about it in Ghostwriter's own editor to unblock it.

Loss visibility: every construct dropped or canonicalized on the HTML->markdown
side — TinyMCE ``data-color``/``style`` highlight spans, non-canonical link
``rel``/``target`` values, an ``<ol type>`` numbering style, class/style/other
cosmetic attributes on any allowed tag, and unresolved references (above) — goes
through the optional ``on_loss`` callback, once per dropped/canonicalized
construct, with a human-readable message. It never changes the output, only
makes the drop visible to the caller instead of silent.

Canonical normalizations: ``html_to_md`` never invents visible content, but it
does canonicalize some HTML shapes that have more than one markdown spelling,
so the SAME markdown always comes back out no matter how the equivalent HTML
was built (each is reported via ``on_loss``, and each keeps visible text
identical — see ``tests/test_converter_property.py`` for the properties
proving this). The complete list:

  1. **Adjacent same-tag inline elements merge.** Two directly-adjacent
     ``<strong>``/``<em>``/``<code>`` siblings (with nothing between them, or —
     ``<strong>``/``<em>`` only — separated by nothing but whitespace-only
     text) merge into one element, absorbing any such whitespace into its
     content (``_merge_adjacent_inline``). A plain (non cross-reference)
     ``<span>`` sitting between two same-tag elements does not block this — it
     is transparent for adjacency purposes, since it renders as nothing but
     its own children anyway (``_flatten_transparent_spans``). A
     structurally-empty ``<strong>``/``<em>``/``<code>`` sitting between two
     same-tag elements is dropped first for the same reason
     (``_drop_structurally_empty_inline``) — see normalization 4 below.
  2. **Adjacent same-tag top-level lists merge.** Two adjacent top-level
     ``<ul>``/``<ol>`` blocks of the same tag merge into one — real CommonMark
     reads two markdown list blocks separated only by a blank line back as a
     single loose list on the very next parse regardless of what grison
     itself wrote, so rendering them as two separate blocks would silently
     drift on the next round trip (``_merge_adjacent_top_level_lists``).
  3. **Whitespace moves out of ``<strong>``/``<em>`` boundaries.** Leading/
     trailing whitespace INSIDE a ``<strong>``/``<em>`` tag moves outside the
     ``**``/``*`` delimiters, since CommonMark's emphasis flanking rule
     requires a delimiter run to sit immediately next to non-whitespace
     content to open/close at all (``_wrap_delim``).
  4. **Whitespace-only (or empty) ``<strong>``/``<em>``/``<code>`` is
     dropped.** There is no markdown delimiter that can wrap nothing (or pure
     whitespace) and mean anything different from the whitespace itself —
     any real whitespace is kept as plain text, only the tag is dropped
     (``_wrap_delim``, ``_fence_code``).
  5. **A list item's content is always ``<p>``-wrapped on push.** Matches
     Ghostwriter's real TipTap list-item schema (``ListItem`` node,
     ``content: 'paragraph block*'`` — a block child is required); an item
     with exactly one paragraph and no nested list still round-trips as a
     bare markdown line, with no visible difference.
  6. **A list nested more than one level deep collapses into that one
     sub-level.** Lists support exactly one level of nesting (a 2-space-
     indented sub-item); a THIRD (or deeper) level found in source HTML
     collapses into the same single sub-level rather than being rejected
     (see "Nesting" above).

Every one of these is a real, reported normalization — never a silent change —
and every one keeps the converter's own output an immediate fixpoint: pushing
and pulling ``html_to_md``'s own output again always returns byte-identical
markdown, not merely "close" after one more round.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from grison.errors import GrisonError
from grison.markdown.refs import LocalRef, RefResolver, RemoteRef


class ConverterError(GrisonError, ValueError):
    """Raised when HTML or markdown outside the tiny closed GW vocabulary is seen."""


_BLOCK_TAGS = {"p", "ul", "ol", "li", "div"}
_INLINE_TAGS = {"strong", "code", "em", "a", "br"}
_UNWRAP_TAGS = {"span"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_ALLOWED_TAGS = _BLOCK_TAGS | _INLINE_TAGS | _UNWRAP_TAGS
# Report-narrative fields (report.extraFields) use the same vocabulary as finding
# fields plus headings — the finding converter rejects headings as a corruption
# tripwire, so heading support is opt-in via ``headings=True`` and never loosens the
# strict finding path.

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
_UNRESOLVED_RE = re.compile(r"^gw:evidence-ref:(?:id=(?P<id>\d+)|name=(?P<name>.*))$", re.DOTALL)
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


def _report_loss(on_loss: Callable[[str], None] | None, msg: str) -> None:
    if on_loss:
        on_loss(msg)


def _esc(text: str) -> str:
    """HTML-escape text/attribute content. ``"`` is escaped too so a URL containing a
    quote can't break out of the ``href="…"`` attribute and inject markup."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Punctuation that markdown-it's inline grammar treats specially — backslash-escaped
# so literal author text round-trips instead of being re-parsed as markup (defect fix:
# "no backslash escapes"). Used only as a fallback (see `_md_escape_run`) when a text
# run doesn't already round-trip as plain text unescaped.
_MD_ESCAPE_CHARS = set("\\`*_[]<&!")


def _md_escape_run(text: str) -> str:
    """Backslash-escape a run of literal HTML text so it survives a markdown-it
    parse unchanged, rather than accidentally forming emphasis/code/links/HTML.

    Verifies first: markdown-it itself is the arbiter of "would this actually be
    reinterpreted" — if ``text`` already parses back to the exact same single
    plain-text token unescaped (the common case — snake_case identifiers, a bare
    run of asterisks with no matching close, literal ``&``/``<`` not shaped like
    an entity/tag), it's returned as-is. Only when parsing it raw would form real
    markup does this fall back to escaping every markdown/HTML-significant
    character in it — broader than strictly necessary in that harder case, but
    always safe.
    """
    if text == "" or _parses_as_plain_text(text):
        return text
    return "".join(f"\\{c}" if c in _MD_ESCAPE_CHARS else c for c in text)


def _parses_as_plain_text(text: str) -> bool:
    tree = _parse_inline_tree(text)
    return len(tree.children) == 1 and tree.children[0].type == "text" and (
        tree.children[0].content == text
    )


_LEAD_TRAIL_WS_RE = re.compile(r"^(\s*)(.*?)(\s*)$", re.DOTALL)


def _wrap_delim(
    marker: str, content: str, tag: str = "", on_loss: Callable[[str], None] | None = None
) -> str:
    """Wrap ``content`` in a ``*``/``**`` delimiter pair, moving any leading/
    trailing whitespace OUTSIDE the delimiters first. CommonMark's emphasis rule
    requires a delimiter run to be immediately adjacent to non-whitespace content
    (a closing ``**`` right after a space isn't "left-flanking" and doesn't
    close); GW's HTML can legitimately have that whitespace INSIDE the
    ``<strong>``/``<em>`` tag (e.g. ``<strong>label: </strong>text``), so it has
    to move to stay a real round-trippable delimiter.

    Whitespace-only (or empty) content has no CommonMark representation at all
    — there's no non-whitespace character for a delimiter to flank — and no
    visual meaning either (bold whitespace looks identical to plain whitespace
    to any reader), so the tag is correctly dropped; reported via ``on_loss``
    like any other dropped construct, real data has this (a bare
    ``<strong> </strong>``)."""
    if content.strip() == "":
        kind = "whitespace-only" if content else "empty"
        _report_loss(on_loss, f"{kind} <{tag}> dropped (no markdown representation)")
        return content
    m = _LEAD_TRAIL_WS_RE.match(content)
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    return f"{lead}{marker}{core}{marker}{trail}"


def _md_escape_quotes(text: str) -> str:
    """Backslash-escape a literal ``"`` in a link title so it can't prematurely
    close the title's own quoted markdown syntax."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


_LINE_START_SIGIL_RE = re.compile(r"^(#{1,6}(?=[ \t]|$)|[-*+](?=[ \t]|$)|\d+\.(?=[ \t]|$)|>|```)")


def _md_escape_line_start(line: str) -> str:
    """Escape a line-leading sequence that would otherwise be read as a block
    sigil (heading/list marker/blockquote/fence) once this text starts a fresh
    output line. Only the first matched character needs the backslash — CommonMark
    treats an escaped punctuation character as ordinary text, which is enough to
    stop the whole-line pattern from matching."""
    m = _LINE_START_SIGIL_RE.match(line)
    if not m:
        return line
    pos = m.end() - 1 if m.group(0) not in ("```",) else 0
    return line[:pos] + "\\" + line[pos:]


def _finalize_line(text: str) -> str:
    """Finalize each physical line of already-rendered inline content (a
    paragraph, or one list item's own line) before it's written out as its own
    markdown line: strip leading AND trailing whitespace (HTML collapses/
    ignores both in normal flow — GW's stored text occasionally has either —
    and left in literally: leading, 4+ spaces reads back as an
    indented-code-block attempt on the next push; trailing, right before an
    embedded hard break's own newline, real CommonMark trims a SINGLE trailing
    space as insignificant before a soft/hard break, silently dropping it on
    the next push — either way exactly backwards for content that was never
    meant to carry that whitespace), then escape a leading block-sigil-looking
    sequence so it can never be misread as a heading/list marker/blockquote/
    fence either."""
    return "\n".join(_md_escape_line_start(line.strip(" ")) for line in text.split("\n"))


def _fence_code(text: str) -> str:
    """Render literal code-span text with a backtick fence one longer than the
    longest run of backticks already in it (CommonMark's own convention for a
    code span whose content contains backticks), padding with a single space on
    any side that starts/ends with a backtick so it can't merge with the fence.

    Empty text has no CommonMark representation at all: an empty fence pair
    (``` `` ```) has no closing run left to match against, so it doesn't parse
    back as a code span — it stays literal backtick characters, silently
    corrupting the round trip. There's no valid non-empty way to represent
    zero-length code content (unlike whitespace, which a single padding space
    can represent), so — matching how an empty ``<strong>``/``<em>`` naturally
    loses its tag when there's nothing to wrap (see ``_wrap_delim``) — empty
    input renders to nothing at all rather than a broken fence.

    CommonMark also trims code-span content that begins AND ends with a space
    character (and isn't all spaces): a single space is removed from each end
    on parse. Left uncompensated, that silently eats real leading/trailing
    whitespace on the very next round trip (``` both ``` -> content "both",
    losing the spaces for good). So once backtick-adjacency padding above has
    settled the content that will actually sit between the fences, if THAT
    content begins and ends with a space (and isn't all spaces), one more
    space is added on each side — CommonMark's parse-time trim then removes
    exactly that compensating pair and leaves the real content untouched."""
    if text == "":
        return ""
    runs = re.findall(r"`+", text)
    fence = "`" * (max((len(r) for r in runs), default=0) + 1)
    if text.startswith("`"):
        text = " " + text
    if text.endswith("`"):
        text = text + " "
    if text.startswith(" ") and text.endswith(" ") and text.strip(" ") != "":
        text = " " + text + " "
    return f"{fence}{text}{fence}"


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


# --- html -> markdown -------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Builds a tiny tree from an HTML fragment, rejecting non-whitelisted tags."""

    def __init__(self, *, headings: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack: list[_Node] = [self.root]
        self._allowed = _ALLOWED_TAGS | _HEADING_TAGS if headings else _ALLOWED_TAGS

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)  # self-closing form, e.g. <br/>

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._allowed:
            raise ConverterError(f"unsupported HTML tag: <{tag}>")
        if tag == "br":
            self.stack[-1].children.append(_Node("br"))
            return
        keep = _KEEP_ATTRS.get(tag, ())
        node_attrs: dict[str, str] = {}
        dropped: list[str] = []
        for name, value in attrs:
            if name in keep:
                node_attrs[name] = value or ""
            elif value:
                dropped.append(f"{name}={value!r}")
        if dropped:
            node_attrs["_dropped"] = " ".join(dropped)
        node = _Node(tag, node_attrs)
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag == "br":
            return
        if tag not in self._allowed:
            raise ConverterError(f"unsupported HTML tag: </{tag}>")
        if len(self.stack) <= 1 or self.stack[-1].tag != tag:
            raise ConverterError(f"mismatched closing tag: </{tag}>")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _report_dropped_attrs(node: _Node, on_loss: Callable[[str], None] | None) -> None:
    dropped = node.attrs.get("_dropped")
    if dropped:
        _report_loss(on_loss, f"<{node.tag}> attribute(s) dropped: {dropped.strip()}")


def html_to_md(
    html: str,
    *,
    headings: bool = False,
    refs: RefResolver | None = None,
    on_loss: Callable[[str], None] | None = None,
) -> str:
    """Convert a GW rich-text HTML fragment to markdown. ``headings=True`` also
    accepts ``<h1>``-``<h6>`` (for report-narrative fields, not finding fields).

    ``refs``, if given, resolves embedded-evidence/cross-reference constructs (see
    the module docstring); without one, encountering any of them raises
    ``ConverterError``. ``on_loss``, if given, is called once per dropped/
    canonicalized construct (styling spans, non-canonical link rel/target, dropped
    attributes, unresolved references) with a human-readable message. It never
    changes the output — the drop still happens — it only makes the drop visible
    to the caller instead of silent.
    """
    if not html.strip():
        return ""
    builder = _TreeBuilder(headings=headings)
    builder.feed(html)
    builder.close()
    if len(builder.stack) != 1:
        raise ConverterError(f"unclosed HTML tag: <{builder.stack[-1].tag}>")
    blocks = _merge_adjacent_top_level_lists(_group_top_level(builder.root.children), on_loss)
    return "\n\n".join(_render_block(block, refs, on_loss) for block in blocks)


def _group_top_level(children: list[_Node | str]) -> list[_Node]:
    """Split top-level children into p/ul/ol/div/heading blocks, wrapping stray
    inline content in an implicit paragraph and dropping insignificant top-level
    whitespace."""
    blocks: list[_Node] = []
    buffer: list[_Node | str] = []

    def flush() -> None:
        if buffer:
            blocks.append(_Node("p", children=list(buffer)))
            buffer.clear()

    for child in children:
        if isinstance(child, str) and child.strip() == "":
            continue
        if isinstance(child, _Node) and (
            child.tag in ("p", "ul", "ol", "div") or child.tag in _HEADING_TAGS
        ):
            flush()
            blocks.append(child)
        else:
            buffer.append(child)
    flush()
    return blocks


def _merge_adjacent_top_level_lists(
    blocks: list[_Node], on_loss: Callable[[str], None] | None
) -> list[_Node]:
    """Two adjacent top-level ``<ul>``/``<ol>`` blocks of the SAME tag merge
    into one — visually identical (a browser renders two immediately-adjacent
    same-type lists exactly like one), and necessary: rendered as two separate
    markdown list blocks separated by a blank line, real CommonMark reads that
    BACK as one loose list (a blank line between two lists of the same marker
    type doesn't start a new list — see the module docstring's list section),
    so grison's OWN un-merged output would silently retighten on the very next
    push/pull round. Reported via ``on_loss`` as a normalization."""
    merged: list[_Node] = []
    for block in blocks:
        if (
            merged
            and block.tag in ("ul", "ol")
            and merged[-1].tag == block.tag
        ):
            prev = merged[-1]
            merged[-1] = _Node(
                prev.tag, dict(prev.attrs), list(prev.children) + list(block.children)
            )
            _report_loss(
                on_loss, f"adjacent <{block.tag}> lists merged into one (normalization)"
            )
            continue
        merged.append(block)
    return merged


def _is_bare_text(children: list[_Node | str]) -> str | None:
    """If ``children`` is exactly one text node (ignoring nothing else at all),
    return its content; else ``None``."""
    if len(children) == 1 and isinstance(children[0], str):
        return children[0]
    return None


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
        raise ConverterError(
            "evidence reference found but no RefResolver was given "
            "(pass refs= to html_to_md to resolve it, or drop the reference)"
        )
    local = refs.to_local(remote)
    if local is None:
        marker = f"id={remote.id}" if remote.id is not None else f"name={remote.name}"
        _report_loss(on_loss, f"unresolved reference: gw-evidence {marker}")
        return _fence_code(f"gw:evidence-ref:{marker}")
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


def _render_block(
    node: _Node, refs: RefResolver | None = None, on_loss: Callable[[str], None] | None = None
) -> str:
    if node.tag == "p":
        text = _is_bare_text(node.children)
        if text is not None:
            embed = _try_render_dot_embed(text, refs, on_loss)
            if embed is not None:
                return embed
        _report_dropped_attrs(node, on_loss)
        return _finalize_line(_render_inline(node.children, refs, on_loss))
    if node.tag in _HEADING_TAGS:
        _report_dropped_attrs(node, on_loss)
        return "#" * int(node.tag[1]) + " " + _render_inline(node.children, refs, on_loss)
    if node.tag in ("ul", "ol"):
        return _render_list_node(node, refs, on_loss)
    if node.tag == "div":
        return _render_evidence_div(node, refs, on_loss)
    raise ConverterError(f"unsupported block-level tag: <{node.tag}>")


def _ol_start(node: _Node, on_loss: Callable[[str], None] | None) -> int:
    """Numbering start for an ``<ol>`` — ``start`` if given (else 1). ``type``
    (a/i/A/I numbering) can't be represented in markdown, so it's dropped and
    reported via ``on_loss`` — items are always rendered as decimal ``N. ``."""
    type_attr = node.attrs.get("type")
    if type_attr:
        _report_loss(
            on_loss, f"<ol type={type_attr!r}> numbering style dropped (rendered as 1. 2. 3. ...)"
        )
    start_attr = node.attrs.get("start")
    if start_attr:
        try:
            return int(start_attr)
        except ValueError:
            pass
    return 1


def _render_list_node(
    node: _Node, refs: RefResolver | None = None, on_loss: Callable[[str], None] | None = None
) -> str:
    is_ol = node.tag == "ol"
    n = _ol_start(node, on_loss) if is_ol else 1
    _report_dropped_attrs(node, on_loss)
    lines = []
    for li in node.children:
        if isinstance(li, str):
            if li.strip() == "":
                continue
            raise ConverterError(f"stray text directly inside <{node.tag}> (expected <li>)")
        if li.tag != "li":
            raise ConverterError(f"unsupported <{node.tag}> child: <{li.tag}>")
        _report_dropped_attrs(li, on_loss)
        item_lines = _render_li(li, refs, on_loss)
        marker = f"{n}. " if is_ol else "- "
        lines.append(marker + item_lines[0])
        # CommonMark requires a nested block to be indented to (at least) this
        # item's OWN marker width to actually belong to it — grison's own output
        # must be real, previewable CommonMark (an AI author previewing it in any
        # CommonMark renderer must see the same nesting grison itself will re-read
        # on push), so the indent is marker-width, not a flat convention.
        indent = " " * len(marker)
        lines.extend(indent + m if m else "" for m in item_lines[1:])
        if is_ol:
            n += 1
    return "\n".join(lines)


_LIST_TAGS = ("ul", "ol")
_LI_BLOCK_TAGS = ("p", "div")


def _render_li(
    li: _Node,
    refs: RefResolver | None = None,
    on_loss: Callable[[str], None] | None = None,
    depth: int = 0,
) -> list[str]:
    """Render one ``<li>``'s content, unwrapping the ``<p>`` GW wraps item content
    in. Returns the item's OWN physical output lines, not yet marker-prefixed —
    the caller adds the ``- ``/``N. `` marker to line 0 and indents the rest to
    match (nested nested sub-items' own markers, or a blank line + indented
    continuation block for a multi-block/loose item — see module docstring).

    A bare inline ``<li>`` (no ``<p>``/``<div>`` child at all — what THIS converter
    itself emits for a single-block item) and an ``<li>`` with exactly one ``<p>``
    (with or without a nested list) both render as one bare line: the corpus
    impact/mitigation/references fields are ``<ul><li><p>…</p></li></ul>``, so
    whitespace round-trips exactly rather than being paragraph-joined/stripped.
    Two or more blocks (multiple ``<p>``, or a ``<p>``/embed ``<div>`` mix) render
    as a loose item: each extra block becomes a blank line then an indented
    continuation line.
    """
    blocks = [c for c in li.children if isinstance(c, _Node) and c.tag in _LI_BLOCK_TAGS]
    lists = [c for c in li.children if isinstance(c, _Node) and c.tag in _LIST_TAGS]
    nested: list[str] = []
    for lst in lists:
        _report_dropped_attrs(lst, on_loss)
        nested.extend(_flatten_nested_list(lst, refs, on_loss, depth + 1))

    if not blocks:
        inline_children = [
            c for c in li.children if not (isinstance(c, _Node) and c.tag in _LIST_TAGS)
        ]
        head = _finalize_line(_render_inline(inline_children, refs, on_loss))
        return [head, *nested]
    if len(blocks) == 1 and blocks[0].tag == "p":
        _report_dropped_attrs(blocks[0], on_loss)
        head = _finalize_line(_render_inline(blocks[0].children, refs, on_loss))
        return [head, *nested]

    lines: list[str] = []
    for b in blocks:
        _report_dropped_attrs(b, on_loss)
        if b.tag == "div":
            text = _render_evidence_div(b, refs, on_loss)
        else:
            bare_text = _is_bare_text(b.children)
            embed = (
                _try_render_dot_embed(bare_text, refs, on_loss)
                if bare_text is not None
                else None
            )
            if embed is not None:
                text = embed
            else:
                text = _finalize_line(_render_inline(b.children, refs, on_loss))
        if lines:
            lines.append("")
        lines.append(text)
    lines.extend(nested)
    return lines


def _flatten_nested_list(
    lst: _Node,
    refs: RefResolver | None,
    on_loss: Callable[[str], None] | None,
    depth: int = 1,
) -> list[str]:
    """Flatten a ``<ul>``/``<ol>`` nested inside an ``<li>`` — and anything nested
    inside IT — into a flat list of marker-prefixed sub-item lines, all at the one
    supported nesting level (the indent, matching the OUTER item's own marker
    width, is applied by the caller). ``depth`` counts nesting levels from 1 (the
    one level markdown itself can express); depth 2+ is a 3rd-or-deeper level
    already present in the source HTML being collapsed into that same one level —
    reported via ``on_loss`` since it's a real, if rare, loss of structure (the
    markdown side hard-rejects an attempt to author one instead — see
    ``_render_list_item_node``)."""
    if depth >= 2:
        _report_loss(
            on_loss,
            f"<{lst.tag}> nested {depth + 1} levels deep collapsed into the one supported "
            "sub-level",
        )
    is_ol = lst.tag == "ol"
    n = _ol_start(lst, on_loss) if is_ol else 1
    lines: list[str] = []
    for li in lst.children:
        if isinstance(li, str):
            if li.strip() == "":
                continue
            raise ConverterError(f"stray text directly inside <{lst.tag}> (expected <li>)")
        if li.tag != "li":
            raise ConverterError(f"unsupported <{lst.tag}> child: <{li.tag}>")
        _report_dropped_attrs(li, on_loss)
        item_lines = _render_li(li, refs, on_loss, depth)
        if item_lines and item_lines[0]:
            marker = f"{n}. " if is_ol else "- "
            lines.append(marker + item_lines[0])
            # A 3rd+ level's own lines are already fully formed (marker-prefixed)
            # by a deeper call to this same function — added verbatim, NOT
            # re-indented, so indent is applied exactly once (by the outermost
            # `_render_list_node`), collapsing every deeper level into this one.
            lines.extend(item_lines[1:])
            if is_ol:
                n += 1
        else:
            lines.extend(item_lines[1:])
    return lines


def _render_inline(
    nodes: list[_Node | str],
    refs: RefResolver | None = None,
    on_loss: Callable[[str], None] | None = None,
) -> str:
    parts = []
    for n in _merge_adjacent_inline(nodes, on_loss):
        if isinstance(n, str):
            parts.append(_render_text_run(n, refs, on_loss))
        elif n.tag == "br":
            parts.append("\n")
        elif n.tag == "strong":
            _report_dropped_attrs(n, on_loss)
            inner = _render_inline(n.children, refs, on_loss)
            parts.append(_wrap_delim("**", inner, "strong", on_loss))
        elif n.tag == "em":
            _report_dropped_attrs(n, on_loss)
            inner = _render_inline(n.children, refs, on_loss)
            parts.append(_wrap_delim("*", inner, "em", on_loss))
        elif n.tag == "code":
            _report_dropped_attrs(n, on_loss)
            parts.append(_fence_code(_render_code_text(n.children)))
        elif n.tag == "a":
            href = n.attrs.get("href", "")
            rel = n.attrs.get("rel")
            if rel is not None and rel.strip() != "noopener":
                _report_loss(on_loss, f'link rel={rel!r} canonicalized to "noopener" on push')
            target = n.attrs.get("target")
            if target is not None and target.strip() != "_blank":
                _report_loss(
                    on_loss, f'link target={target!r} canonicalized to "_blank" on push'
                )
            _report_dropped_attrs(n, on_loss)
            title = n.attrs.get("title")
            title_part = f' "{_md_escape_quotes(title)}"' if title else ""
            parts.append(f"[{_render_inline(n.children, refs, on_loss)}]({href}{title_part})")
        elif n.tag == "span":
            # A plain (non cross-reference) <span> is always spliced away by
            # _flatten_transparent_spans() inside _merge_adjacent_inline()
            # above, before this dispatch ever sees it — so the only <span>
            # that can reach here is the opaque cross-reference marker.
            parts.append(_render_cross_ref_span(n, refs, on_loss))
        else:
            raise ConverterError(f"unsupported tag in inline content: <{n.tag}>")
    return "".join(parts)


_MERGEABLE_TAGS = frozenset({"strong", "em", "code"})


def _is_structurally_empty(node: _Node | str) -> bool:
    """True if ``node`` contributes no visible markdown content at all once
    fully collapsed — purely a structural check on the tree (no render, no
    ``RefResolver`` needed), used to strip out-of-the-way "invisible" nodes
    BEFORE the adjacency scan below so two real ``<strong>``/``<em>`` siblings
    separated only by e.g. an empty ``<em></em>`` are still recognized as
    adjacent and merged. Whitespace-only text and further structurally-empty
    ``<strong>``/``<em>`` wrappers count as empty; a truly-empty ``<code>``
    (matching ``_fence_code``'s own empty-string drop — whitespace-only code
    text is real, preserved content, NOT empty) counts as empty too. Anything
    else — a link, image, reference, ``<br>``, real text — is content, so this
    stays conservative and never mistakes it for empty."""
    if isinstance(node, str):
        return node.strip() == ""
    if node.tag in ("strong", "em"):
        return all(_is_structurally_empty(c) for c in node.children)
    if node.tag == "code":
        return not node.children or all(isinstance(c, str) and c == "" for c in node.children)
    return False


def _flatten_structural_text(node: _Node | str) -> str:
    """Concatenate every leaf text string under a structurally-empty
    ``<strong>``/``<em>``/``<code>`` node (all whitespace or nothing, by
    ``_is_structurally_empty``'s contract) — used only to tell "empty" from
    "whitespace-only" in the drop message below."""
    if isinstance(node, str):
        return node
    return "".join(_flatten_structural_text(c) for c in node.children)


def _flatten_transparent_spans(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Splice a plain (non cross-reference) ``<span>`` in place with its own
    (recursively flattened) children before the adjacency scan below, since
    ``_render_inline`` itself later unwraps such a span to nothing but its
    children's rendering anyway — leaving it in the sibling list would
    otherwise block two REAL same-tag elements on either side of it from
    being seen as adjacent, exactly like a structurally-empty wrapper does
    (see ``_is_structurally_empty``), even though this span's own content is
    not empty (``<strong>a</strong><span><strong>b</strong></span>`` must
    merge into one ``<strong>a b</strong>`` just as if the span weren't
    there). A ``<span>`` carrying the cross-reference marker attrs is NOT
    flattened: it renders as one opaque unit (see ``_render_cross_ref_span``),
    never decomposed. This is also now where the "styling span dropped"/
    dropped-attrs diagnostics for a plain span are reported, since a
    flattened span is never seen by ``_render_inline``'s own per-node
    dispatch again."""
    out: list[_Node | str] = []
    for node in nodes:
        if (
            isinstance(node, _Node)
            and node.tag == "span"
            and _GW_REF_ENCODED_ATTR not in node.attrs
            and "data-gw-ref" not in node.attrs
        ):
            attrs = {k: v for k, v in node.attrs.items() if v and k != "_dropped"}
            if attrs:
                shown = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
                _report_loss(on_loss, f"styling span dropped ({shown})")
            _report_dropped_attrs(node, on_loss)
            out.extend(_flatten_transparent_spans(node.children, on_loss))
            continue
        out.append(node)
    return out


def _drop_structurally_empty_inline(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Remove the TAG from top-level ``<strong>``/``<em>``/``<code>`` siblings
    that are structurally empty (see ``_is_structurally_empty``), reporting
    each one via ``on_loss`` exactly as ``_wrap_delim``/``_fence_code`` would
    at render time — this runs first specifically so the adjacency merge
    below sees real content as truly adjacent. Any whitespace text the
    dropped tag was wrapping is kept as a plain sibling in its place (just
    like ``_wrap_delim`` returns its whitespace ``content`` unwrapped rather
    than deleting it — a bare ``<strong> </strong>`` between two words is
    still a real space separating them, not nothing). Anything that slips
    through this structural check (e.g. a wrapper that renders empty only
    after a reference resolves) is still caught later by ``_wrap_delim``/
    ``_fence_code`` themselves as a safety net."""
    kept: list[_Node | str] = []
    for node in nodes:
        is_mergeable_empty = (
            isinstance(node, _Node)
            and node.tag in ("strong", "em", "code")
            and _is_structurally_empty(node)
        )
        if is_mergeable_empty:
            assert isinstance(node, _Node)
            if node.tag == "code":
                # an empty <code> already renders to "" silently (see
                # _fence_code) with nothing meaningful to report beyond that
                continue
            text = _flatten_structural_text(node)
            rendered_kind = "whitespace-only" if text else "empty"
            _report_loss(
                on_loss, f"{rendered_kind} <{node.tag}> dropped (no markdown representation)"
            )
            if text:
                kept.append(text)
            continue
        kept.append(node)
    return kept


def _merge_dropped_attrs(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    merged = dict(a)
    b_dropped = b.get("_dropped")
    if b_dropped:
        merged["_dropped"] = (merged.get("_dropped", "") + " " + b_dropped).strip()
    return merged


def _coalesce_adjacent_strings(nodes: list[_Node | str]) -> list[_Node | str]:
    """Merge consecutive plain-text siblings into one string. Dropping a
    structurally-empty element (see ``_drop_structurally_empty_inline``) that
    sat between two whitespace text nodes leaves them directly adjacent in
    the list; the one-whitespace-node lookahead in the merge loop below only
    ever looks past a SINGLE text sibling, so without this they'd stay two
    separate list entries and silently double the whitespace between two
    real elements that should otherwise merge across it."""
    out: list[_Node | str] = []
    for node in nodes:
        if isinstance(node, str) and out and isinstance(out[-1], str):
            out[-1] = out[-1] + node
        else:
            out.append(node)
    return out


def _merge_adjacent_inline(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Normalize the HTML tree BEFORE rendering: two directly-adjacent
    ``<strong>``/``<em>``/``<code>`` siblings of the same tag — with nothing
    between them, or (``<strong>``/``<em>`` only) separated by nothing but
    whitespace-only text — merge into ONE element (absorbing any such
    whitespace into its content), reported via ``on_loss`` as a normalization.

    This is the real fix for the ambiguity two adjacent same-delimiter runs
    create on the next markdown parse (``**a**`` + ``**b**`` naively
    concatenated is ``**a****b**``, which CommonMark reads as one ``<strong>``
    spanning ``a****b``, not two) — merging the SOURCE TREE first means
    ``_render_inline`` only ever renders ONE real ``<strong>a b</strong>``,
    visually identical to the two-element original, with no invented
    characters of any kind needed in the output.

    A structurally-empty ``<strong>``/``<em>``/``<code>`` sibling sitting
    between two same-tag elements (e.g. an author-written ``<em></em>``
    between two ``<strong>``s) is dropped FIRST (see
    ``_drop_structurally_empty_inline``), so it can never block this merge —
    otherwise the two ``<strong>``s would render unmerged here but merge
    anyway on the very next round trip (once the empty ``<em>`` has no HTML
    representation left to round-trip through), breaking the fixpoint
    property. A plain (non cross-reference) ``<span>`` sibling is similarly
    spliced away first by ``_flatten_transparent_spans`` — it renders as
    nothing but its own children anyway, so it must not block adjacency
    either."""
    nodes = _flatten_transparent_spans(nodes, on_loss)
    nodes = _coalesce_adjacent_strings(_drop_structurally_empty_inline(nodes, on_loss))
    merged: list[_Node | str] = []
    i = 0
    n = len(nodes)
    while i < n:
        node = nodes[i]
        if isinstance(node, _Node) and node.tag in _MERGEABLE_TAGS:
            combined = node
            j = i + 1
            while j < n:
                nxt = nodes[j]
                if isinstance(nxt, _Node) and nxt.tag == combined.tag:
                    combined = _Node(
                        combined.tag,
                        _merge_dropped_attrs(combined.attrs, nxt.attrs),
                        list(combined.children) + list(nxt.children),
                    )
                    _report_loss(
                        on_loss,
                        f"adjacent <{combined.tag}> elements merged into one (normalization)",
                    )
                    j += 1
                    continue
                if (
                    combined.tag in ("strong", "em")
                    and isinstance(nxt, str)
                    and nxt.strip() == ""
                    and j + 1 < n
                    and isinstance(nodes[j + 1], _Node)
                    and nodes[j + 1].tag == combined.tag  # type: ignore[union-attr]
                ):
                    after = nodes[j + 1]
                    assert isinstance(after, _Node)
                    combined = _Node(
                        combined.tag,
                        _merge_dropped_attrs(combined.attrs, after.attrs),
                        [*combined.children, nxt, *after.children],
                    )
                    _report_loss(
                        on_loss,
                        f"adjacent <{combined.tag}> elements merged into one (normalization)",
                    )
                    j += 2
                    continue
                break
            merged.append(combined)
            i = j
        else:
            merged.append(node)
            i += 1
    return merged


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
        raise ConverterError(
            "cross-reference found but no RefResolver was given "
            "(pass refs= to html_to_md to resolve it, or drop the reference)"
        )
    local = refs.to_local(RemoteRef("gw-evidence", None, name, None))
    if local is None:
        _report_loss(on_loss, f"unresolved reference: gw-evidence name={name}")
        return _fence_code(f"gw:evidence-ref:name={name}")
    return f"[{_md_escape_run(_ref_display_text(local))}]({local.path})"


def _render_text_run(
    text: str, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    """Render one literal HTML text node as markdown: a D10 escape token (see
    ``_jinja_escape_html``) unwraps to the literal delimiter text it stands
    for first, then legacy ``.ref`` cross-refs, then active (un-escaped)
    Jinja delimiters (see module docstring); anything else is escaped
    literal text."""
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


def _render_dot_form_inline(
    contents: str, refs: RefResolver | None, on_loss: Callable[[str], None] | None
) -> str:
    contents = contents.strip()
    if contents.startswith("ref "):
        name = contents[4:].strip()
        if refs is None:
            raise ConverterError(
                "legacy {{.ref}} reference found but no RefResolver was given "
                "(pass refs= to html_to_md to resolve it, or drop the reference)"
            )
        local = refs.to_local(RemoteRef("gw-evidence", None, name, None))
        if local is None:
            _report_loss(on_loss, f"unresolved reference: gw-evidence name={name}")
            return _fence_code(f"gw:evidence-ref:name={name}")
        return f"[{_md_escape_run(_ref_display_text(local))}]({local.path})"
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


def _render_code_text(nodes: list[_Node | str]) -> str:
    """<code> content is never inline-parsed, so just flatten its text (unwrapping
    any cosmetic <span>, and unwrapping each D10 escape token — see
    ``_jinja_escape_html`` — back to the literal delimiter text it stands for —
    but rejecting any other nested tag). Content inside <code> is always
    literal: an active (un-escaped) template expression here is not
    distinguished from literal braces (see module docstring)."""
    parts = []
    for n in nodes:
        if isinstance(n, str):
            parts.append(_JINJA_STRLIT_RE.sub(lambda m: m.group(1), n))
        elif n.tag == "span":
            parts.append(_render_code_text(n.children))
        else:
            raise ConverterError(f"unsupported nested tag inside <code>: <{n.tag}>")
    return "".join(parts)


# --- markdown -> html --------------------------------------------------------
#
# The WHOLE document is parsed once with markdown-it-py's real CommonMark block
# parser (nothing disabled, no plugin enabled) into a SyntaxTreeNode tree, which
# is walked and validated against grison's allow-list — every block/inline node
# type not on it raises ConverterError naming the construct and its source line
# (from the token's ``.map``) — then rendered with grison's OWN renderer (never
# markdown-it's HTML renderer), so the output HTML stays byte-for-byte in
# Ghostwriter's canonical shape. The only thing CommonMark itself has no concept
# of at all (no table extension is enabled) is a pipe table's separator row,
# which just becomes ordinary paragraph text to the parser — that ONE construct
# is still checked against the paragraph's raw source lines directly (see
# ``_check_not_table``), since no tree node exists to check instead.

_MD = MarkdownIt("commonmark")
_TABLE_SEP_RE = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?")
_ATX_MARKUP = frozenset({"#", "##", "###", "####", "#####", "######"})


def md_to_html(
    md: str, *, headings: bool = False, refs: RefResolver | None = None, jinja_escape: bool = True
) -> str:
    """Convert markdown (the tiny closed GW subset, plus the reference/template
    special forms) to an HTML fragment. ``headings=True`` also accepts ATX
    headings ``# ``-``###### `` (for report-narrative fields). ``refs``, if given,
    resolves embed/cross-reference markdown into Ghostwriter's native HTML forms
    (see module docstring); without one, any of them raises ``ConverterError``.
    ``jinja_escape=True`` (the default, for Ghostwriter-bound text) wraps literal
    template delimiters in author text so Ghostwriter's Jinja compiler renders
    them back literally (D10) — see module docstring for the escape form and the
    reserved ``gw:`` inline-code forms that bypass it.
    """
    if not md.strip():
        return ""
    normalized = md.replace("\r\n", "\n")
    env: dict = {}
    tokens = _MD.parse(normalized, env)
    if env.get("references"):
        # A link reference definition ("[foo]: url") is consumed by markdown-it's
        # "reference" block rule with NO token emitted at all — silently dropping
        # it would violate "hard rejection, never silent"; catch it here since
        # there's no tree node to reject it from.
        name = next(iter(env["references"]))
        raise ConverterError(f"unsupported markdown: link reference definition ({name!r})")
    tree = SyntaxTreeNode(tokens)
    lines = normalized.split("\n")
    blocks = [
        _render_block_node(
            child, headings=headings, refs=refs, jinja_escape=jinja_escape, lines=lines, nesting=0
        )
        for child in tree.children
    ]
    return "\n\n".join(b for b in blocks if b)


def _node_line(node: SyntaxTreeNode) -> int:
    """1-based source line for an error message, from the token's own ``.map``."""
    return (node.map[0] + 1) if node.map else 0


def _check_not_table(node: SyntaxTreeNode, lines: list[str]) -> None:
    if not node.map:
        return
    for line in lines[node.map[0] : node.map[1]]:
        if _TABLE_SEP_RE.fullmatch(line.strip()):
            raise ConverterError(f"unsupported markdown: table (line {_node_line(node)}: {line!r})")


def _inline_children(node: SyntaxTreeNode) -> list[SyntaxTreeNode]:
    return node.children[0].children if node.children else []


def _render_block_node(
    node: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    lines: list[str],
    nesting: int,
) -> str:
    if node.type == "paragraph":
        _check_not_table(node, lines)
        return _render_paragraph_node(node, refs=refs, jinja_escape=jinja_escape)
    if node.type == "heading":
        if not headings:
            raise ConverterError(f"unsupported markdown: ATX heading (line {_node_line(node)})")
        if node.markup not in _ATX_MARKUP:
            raise ConverterError(
                f"unsupported markdown: setext heading underline (line {_node_line(node)})"
            )
        level = node.tag[1]
        inner = _render_inline_nodes(
            _inline_children(node), refs, jinja_escape, line=_node_line(node)
        )
        return f"<h{level}>{inner}</h{level}>"
    if node.type in ("bullet_list", "ordered_list"):
        return _render_list_node2(
            node,
            headings=headings,
            refs=refs,
            jinja_escape=jinja_escape,
            lines=lines,
            nesting=nesting,
        )
    name = _BLOCK_TYPE_NAMES.get(node.type, node.type)
    raise ConverterError(f"unsupported markdown: {name} (line {_node_line(node)})")


_BLOCK_TYPE_NAMES = {
    "hr": "thematic break",
    "blockquote": "blockquote",
    "fence": "fenced code block",
    "code_block": "indented code block",
    "html_block": "raw HTML block",
}


def _render_paragraph_node(
    node: SyntaxTreeNode, *, refs: RefResolver | None, jinja_escape: bool
) -> str:
    """A paragraph whose ONLY inline content is a single image is the embed form
    (D1/D9); one whose only content is a reserved unresolved-reference marker
    (see module docstring) reconstructs that construct directly — both are their
    own block (top-level, or a direct child block of a list item), never wrapped
    in ``<p>``. Every other paragraph — including every list-item paragraph —
    renders wrapped in ``<p>``: Ghostwriter's real TipTap list-item schema always
    contains at least one paragraph block (a ProseMirror ``listItem`` node's
    content is block-only — bare inline ``<li>`` text is never actually stored),
    so grison's canonical push output matches that unconditionally rather than
    reproducing CommonMark's own tight/loose distinction, which Ghostwriter's
    editor has no concept of at all."""
    inline_children = _inline_children(node)
    if len(inline_children) == 1:
        only = inline_children[0]
        if only.type == "image":
            return _push_embed_ref(only.attrs.get("src", "") or "", refs)
        if only.type == "code_inline":
            m = _UNRESOLVED_RE.match(only.content)
            if m:
                return _render_unresolved_marker(m, position="block")
    line = _node_line(node)
    return f"<p>{_render_inline_nodes(inline_children, refs, jinja_escape, line=line)}</p>"


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
        raise ConverterError(
            "image/evidence embed found but no RefResolver was given "
            "(pass refs= to md_to_html to resolve it, or remove the image)"
        )
    remote = refs.to_remote(src)
    if remote is None or remote.id is None:
        raise ConverterError(f"evidence path does not resolve via refs: {src!r}")
    return f'<div class="{_EVIDENCE_DIV_CLASS}" {_EVIDENCE_ID_ATTR}="{remote.id}"></div>'


def _render_list_node2(
    node: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    lines: list[str],
    nesting: int,
) -> str:
    """Render a ``bullet_list``/``ordered_list`` node. One level of nesting is
    supported: an item's OWN nested list renders inside its ``<li>`` (``nesting``
    goes from 0 at the top level to 1 there); a list nested inside THAT one
    (``nesting`` would reach 2) is a hard ``ConverterError`` naming the line — the
    HTML->markdown side still collapses a real GW record's 3rd+ level into that
    same one level (with ``on_loss``) since existing corpus data has it, but
    freshly-authored markdown must not grow a level nothing can round-trip."""
    is_ol = node.type == "ordered_list"
    tag = "ol" if is_ol else "ul"
    start = node.attrs.get("start")
    attr = f' start="{start}"' if is_ol and start not in (None, 1) else ""
    items = [
        _render_list_item_node(
            li,
            headings=headings,
            refs=refs,
            jinja_escape=jinja_escape,
            lines=lines,
            nesting=nesting,
        )
        for li in node.children
    ]
    return f"<{tag}{attr}>{''.join(items)}</{tag}>"


def _render_list_item_node(
    li: SyntaxTreeNode,
    *,
    headings: bool,
    refs: RefResolver | None,
    jinja_escape: bool,
    lines: list[str],
    nesting: int,
) -> str:
    blocks_html: list[str] = []
    nested_html = ""
    for child in li.children:
        if child.type in ("bullet_list", "ordered_list"):
            if nesting >= 1:
                raise ConverterError(
                    "unsupported markdown: list nested more than one level deep "
                    f"(line {_node_line(child)})"
                )
            nested_html += _render_list_node2(
                child,
                headings=headings,
                refs=refs,
                jinja_escape=jinja_escape,
                lines=lines,
                nesting=nesting + 1,
            )
        elif child.type == "paragraph":
            _check_not_table(child, lines)
            blocks_html.append(_render_paragraph_node(child, refs=refs, jinja_escape=jinja_escape))
        else:
            raise ConverterError(
                f"unsupported markdown: {child.type} inside a list item "
                f"(line {_node_line(child)})"
            )
    return f"<li>{''.join(blocks_html)}{nested_html}</li>"


def _parse_inline_tree(text: str) -> SyntaxTreeNode:
    """Parse a standalone snippet of inline markdown (not a document) — used only
    by ``_md_escape_run``'s round-trip-safety check on the HTML->markdown side."""
    tokens = _MD.parseInline(text, {})
    return SyntaxTreeNode(tokens).children[0]  # unwrap the single "inline" token


def _md_to_seminode(
    nodes: list[SyntaxTreeNode], refs: RefResolver | None, jinja_escape: bool, line: int
) -> list[_Node | str]:
    """Convert markdown-it's flat inline node list into grison's OWN ``_Node``/
    str tree (the SAME representation the html->md side builds from real HTML),
    resolving the ``***text***`` em(strong)->strong(em) swap immediately (so the
    SAME tag applies regardless of source shape) — so the ALREADY-PROVEN
    ``_merge_adjacent_inline`` normalization (shared with html_to_md, see its
    docstring) can run on markdown-it-sourced content too, recursively and
    correctly: reusing it here (rather than a second, string-surgery-based
    merge mechanism) is what makes a CASCADING case work — e.g. two adjacent
    ``***a*** ***b***`` runs merge at the outer ``<strong>`` level, which then
    exposes their INNER ``<em>a</em>``/``<em>b</em>`` as newly-adjacent
    siblings needing a SECOND merge too; ``_merge_adjacent_inline`` already
    handles that because ``_render_seminode_html`` re-invokes it every time it
    recurses into a node's (possibly just-combined) children, exactly like
    ``_render_inline`` already does on the html->md side.

    A link, an active/unresolved ``gw:`` code marker, or anything else with its
    own specialized render path is fully rendered to a final HTML string
    immediately and wrapped as an opaque ``_Node("_raw", ..., [html])`` leaf —
    its own inner content already went through this same merge machinery via
    its own render call, so nothing is lost, and it's never itself a merge
    target (a link's ``[`` immediately makes any adjacent same-tag concern
    moot)."""
    out: list[_Node | str] = []
    for n in nodes:
        if n.type == "text":
            out.append(n.content)
        elif n.type in ("softbreak", "hardbreak"):
            out.append(_Node("br"))
        elif n.type == "strong":
            out.append(_Node("strong", {}, _md_to_seminode(n.children, refs, jinja_escape, line)))
        elif n.type == "em":
            # ***text*** parses as em(strong(text)) under real CommonMark delimiter
            # resolution; grison's canonical order is <strong><em> (the html->md
            # side never emits the other order) — swap when an em's ENTIRE content
            # is a single strong with nothing else (the only shape a "***"/"___"
            # run can produce; a genuinely mixed nest like "*a **b** c*" has other
            # content in the em besides the strong, so it's left alone).
            inner_nodes = [c for c in n.children if not (c.type == "text" and c.content == "")]
            if len(inner_nodes) == 1 and inner_nodes[0].type == "strong":
                em_wrapped = _Node(
                    "em", {}, _md_to_seminode(inner_nodes[0].children, refs, jinja_escape, line)
                )
                out.append(_Node("strong", {}, [em_wrapped]))
            else:
                out.append(_Node("em", {}, _md_to_seminode(n.children, refs, jinja_escape, line)))
        elif n.type == "code_inline":
            is_special = _UNRESOLVED_RE.match(n.content) or _GW_ACTIVE_EXPR_RE.match(n.content)
            if is_special:
                rendered = _render_md_code_inline_or_special(n.content, jinja_escape)
                out.append(_Node("_raw", {}, [rendered]))
            else:
                out.append(_Node("code", {}, [n.content]))
        elif n.type == "link":
            out.append(_Node("_raw", {}, [_render_md_link(n, refs, jinja_escape, line=line)]))
        elif n.type == "image":
            raise ConverterError(
                "unsupported markdown: image not alone in its paragraph/list item "
                f"(an embed must be its own block — see the module docstring) (line {line})"
            )
        elif n.type == "html_inline":
            raise ConverterError(
                f"unsupported markdown: inline HTML ({n.content!r}) (line {line}) "
                r"— escape a literal '<' as '\<' if this is plain text"
            )
        else:
            raise ConverterError(f"unsupported markdown construct: {n.type} (line {line})")
    return out


def _render_seminode_html(
    items: list[_Node | str], refs: RefResolver | None, jinja_escape: bool, line: int
) -> str:
    items = _merge_adjacent_inline(items, None)
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(_render_md_text(item, refs, jinja_escape))
        elif item.tag == "br":
            out.append("<br>")
        elif item.tag == "_raw":
            out.append(str(item.children[0]))
        elif item.tag in ("strong", "em"):
            inner = _render_seminode_html(item.children, refs, jinja_escape, line)
            out.append(f"<{item.tag}>{inner}</{item.tag}>")
        elif item.tag == "code":
            content = "".join(str(c) for c in item.children)
            out.append(_render_md_code_inline_or_special(content, jinja_escape))
        else:
            raise ConverterError(f"unexpected internal node <{item.tag}>")  # pragma: no cover
    return "".join(out)


def _render_inline_nodes(
    nodes: list[SyntaxTreeNode], refs: RefResolver | None, jinja_escape: bool, *, line: int = 0
) -> str:
    seminode = _md_to_seminode(nodes, refs, jinja_escape, line)
    return _render_seminode_html(seminode, refs, jinja_escape, line)


def _render_md_link(
    node: SyntaxTreeNode, refs: RefResolver | None, jinja_escape: bool, *, line: int = 0
) -> str:
    href = node.attrs.get("href", "") or ""
    if isinstance(href, str) and href.startswith("evidence/"):
        return _push_cross_ref(href, refs)
    title = node.attrs.get("title")
    title_attr = f' title="{_esc(title)}"' if title else ""
    text = _render_inline_nodes(node.children, refs, jinja_escape, line=line)
    return f'<a href="{_esc(href)}"{title_attr} target="_blank" rel="noopener">{text}</a>'


def _push_cross_ref(href: str, refs: RefResolver | None) -> str:
    if refs is None:
        raise ConverterError(
            "cross-reference to an evidence path found but no RefResolver was given "
            "(pass refs= to md_to_html to resolve it, or use a non-evidence link)"
        )
    remote = refs.to_remote(href)
    if remote is None or remote.name is None:
        raise ConverterError(f"evidence path does not resolve via refs: {href!r}")
    return f'<span {_GW_REF_ENCODED_ATTR}="{_encode_gw_ref(remote.name)}"></span>'


_GW_ACTIVE_EXPR_RE = re.compile(
    r"^gw:(?P<expr>\{\{.*\}\}|\{%.*%\}|\{#.*#\})$", re.DOTALL
)


def _render_md_code_inline_or_special(content: str, jinja_escape: bool) -> str:
    """A ``code_inline`` token's content: the reserved ``gw:`` forms (an
    unresolved-reference marker or an active template expression — see module
    docstring) bypass normal ``<code>`` rendering entirely; anything else renders
    as a real, D10-escaped-if-requested code span."""
    m = _UNRESOLVED_RE.match(content)
    if m:
        return _render_unresolved_marker(m, position="inline")
    m2 = _GW_ACTIVE_EXPR_RE.match(content)
    if m2:
        return m2.group("expr")  # emitted un-escaped, verbatim: stays an active expression
    text = _esc(content)
    if jinja_escape:
        text = _jinja_escape_html(text)
    return f"<code>{text}</code>"


def _render_md_text(text: str, refs: RefResolver | None, jinja_escape: bool) -> str:
    """Render a plain inline-text token: the reserved ``gw:`` prefix (active
    template expression / unresolved reference) is recognized as the WHOLE
    token's content only when it exactly matches one of those forms (produced
    only inside a ``code_inline`` token, handled by
    :func:`_render_md_code_inline_or_special` instead) — a bare text node never
    carries it. Ordinary text is HTML-escaped, then D10-escaped if requested."""
    del refs
    escaped = _esc(text)
    if jinja_escape:
        escaped = _jinja_escape_html(escaped)
    return escaped


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
    ``/home/tfp/repos/grison-rework/proofs/d10-jinja-escape-lab.md``). Applied
    identically whether the fragment sits in plain paragraph text or inside a
    ``<code>`` element — Jinja's lexer scans the whole HTML string as one
    template regardless of what tag a substring sits inside, so an inserted
    ``{{ '...' }}`` expression evaluates the same way in either position.
    ``_JINJA_STRLIT_RE`` above is the exact inverse, used to unwrap this form
    back to its literal token on pull."""
    return _JINJA_TOKEN_RE.sub(lambda m: "{{ '" + m.group(0) + "' }}", escaped_text)
