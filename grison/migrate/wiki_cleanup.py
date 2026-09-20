"""One-time legacy-wiki cleanup (D5): a pure, offline, deterministic text
transform that turns a BookStack wiki page body that violates the rework's new
wiki-body rules (no raw HTML, no ``file://`` links, no corruption artifacts, no
zero-width/bidi control characters, no CRLF, no trailing whitespace, images
only as ``![caption](images/<file>)``) into one that doesn't — or, where it
can't safely guess what a human meant, leaves the offending text untouched and
reports it so a human can fix it by hand.

This module never touches the filesystem or the network and never raises on
any input (see ``clean_page``'s docstring). It is meant to be run once per
page, with the resulting diff reviewed by the owner before anything is pushed
back to BookStack (D5: "one-time cleanup ... reviewed as a diff, then no
exemption list ever") — :func:`render_review` renders that diff-adjacent
report.

Design note on markdown-it-py and position tracking
-----------------------------------------------------
Block-level tokens (``html_block``, ``fence``, ``code_block``, ``paragraph``,
``heading``, the "inline" token) all carry an authoritative ``.map`` — a
``[start_line, end_line)`` line range into the source, confirmed empirically
(this holds even for an "inline" token nested inside a list item or
blockquote, which is what makes locating constructs inside a single list item
reliable). markdown-it-py's INLINE child tokens (``text``, ``html_inline``,
``code_inline``, ...), by contrast, carry no position info of their own. This
module locates them by taking the "inline" token's own raw line-range slice of
the source and finding each child's ``.content`` in it left-to-right with a
monotonically advancing cursor — safe because inline tokens are emitted in
source order and, for a single-line inline block (the overwhelming majority of
real content: prose lines, one link per list item, etc.), the "inline" token's
``.content`` is an exact substring of its own source line (verified: for a
list item it is the raw line with the ``- ``/``N. `` marker prefix already
stripped off by markdown-it itself, so searching for it inside the raw line
handles the list/blockquote-prefix case for free). A multi-line inline block
(a paragraph wrapping several physical lines) is only trusted when its
``.content`` is found verbatim in its own line-range slice — true for every
TOP-LEVEL multi-line paragraph (also verified empirically: no prefix
stripping applies there). When a child's content cannot be located this way,
this module never guesses at a position: it leaves that inline block
untouched and, only if something inside it looked like it needed action, adds
an :class:`Issue` explaining that it could not be safely edited.

Known-tag vs. placeholder trap
-------------------------------
CommonMark's inline-HTML grammar recognizes ANY ``<word ...>``-shaped run as
inline HTML, regardless of whether ``word`` is a real element name — so
``<domain>``, `<user>``, ``<password>``, ``<ip>``, ``<hash>``, ``<target>``
(command-cheatsheet placeholders, never HTML) parse exactly the same way a
real ``<span>`` does. This module's own tag scanner (see ``_parse_fragment``)
resolves the trap the same way: every tag-shaped token is checked against
:data:`_KNOWN_HTML_TAGS` (the WHATWG HTML living-standard element name list,
below); a name that is NOT on that list is never treated as a structural tag
at all — it is left in the output as plain, byte-identical text and is never
reported as a change, an issue, or even considered for conversion. A tag whose
name IS on the list but that this module doesn't know how to convert safely
(``<iframe>``, ``<script>``, a heading tag inside a table cell, ...) becomes
an :class:`Issue` instead of being guessed at.
"""

from __future__ import annotations

import bisect
import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.request import url2pathname

from markdown_it import MarkdownIt

# --- public result types -----------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One transformation this module made. ``before``/``after`` are short
    (<=80 char) excerpts, not necessarily the whole affected span — enough for
    a human reviewing the diff to recognize the spot; the full diff is the
    caller's own diff of ``CleanResult.text`` against the original."""

    rule: str
    line: int
    before: str
    after: str


@dataclass(frozen=True)
class Issue:
    """Something that violates the new wiki-body rules that this module
    refuses to guess at. A human has to look at ``line`` and act."""

    line: int
    explanation: str


@dataclass(frozen=True)
class ImageRef:
    """An absolute own-wiki gallery image URL found in the page, with the
    local filename this module proposes for it under ``images/``. Rewriting
    the reference itself needs a caller-supplied ``url -> local path``
    mapping (see ``clean_page``'s ``image_map``) because only the caller has
    network access to actually download the file first."""

    url: str
    proposed_filename: str


@dataclass(frozen=True)
class CleanResult:
    text: str
    changes: list[Change] = field(default_factory=list)
    unresolved: list[Issue] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)


# --- WHATWG known HTML element names -----------------------------------------

# The WHATWG HTML living-standard element name list (https://html.spec.whatwg.org/
# multipage/indices.html#elements-3), lower-cased. Deliberately does NOT include
# made-up/placeholder-shaped words like "domain", "user", "password", "ip",
# "hash", "target" — those are exactly the placeholders command cheatsheets use,
# and must never be mistaken for real markup (see module docstring).
_KNOWN_HTML_TAGS: frozenset[str] = frozenset(
    """
    a abbr address area article aside audio b base bdi bdo blockquote body br
    button canvas caption cite code col colgroup data datalist dd del details
    dfn dialog div dl dt em embed fieldset figcaption figure footer form h1 h2
    h3 h4 h5 h6 head header hgroup hr html i iframe img input ins kbd label
    legend li link main map mark menu meta meter nav noscript object ol
    optgroup option output p param picture pre progress q rp rt ruby s samp
    script search section select slot small source span strong style sub
    summary sup table tbody td template textarea tfoot th thead time title tr
    track u ul var video wbr
    """.split()
)

# Elements with no closing tag / no content model — never pushed onto the
# open-tag stack even without an explicit self-closing slash.
_VOID_TAGS: frozenset[str] = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

# --- rule names (exported as plain strings so tests can assert on them) -----

R_CRLF = "crlf-normalized"
R_CONTROL_CHAR = "control-char-removed"
R_NBSP = "nbsp-normalized"
R_TRAILING_WS = "trailing-whitespace-stripped"
R_BLANK_LINES = "blank-lines-collapsed"
R_FINAL_NEWLINE = "final-newline-fixed"
R_TITLE_H1 = "title-heading-removed"
R_HTML_TABLE = "html-table-converted"
R_HTML_INLINE = "html-inline-converted"
R_ARTIFACT = "corruption-artifact-repaired"
R_ARTIFACT_HEADING = "corruption-artifact-heading-restored"
R_FILE_LINK = "file-link-stripped"
R_BARE_LINK = "bare-link-unwrapped"
R_GALLERY_IMAGE = "gallery-image-rewritten"
R_BLOCK_DRIFT = "block-drift-escaped"

# --- corruption artifacts, reproduced verbatim from grison/remote/methodology.py --
#
# ``_ARTIFACT_RES`` there: "Literal artifacts that signal a markdown page got
# corrupted (from the migration lesson)." The two patterns this module repairs
# (the truncated-link heuristic in that list is fuzzy/non-blocking and is not a
# "raw HTML" violation of any new wiki rule, so it is out of scope here):
#   - ``<span class="citation...`` — a leaked TinyMCE/Word citation-footnote
#     span that should just be its own visible text, no wrapper.
#   - ``<div class="notice...`` — a leaked notice/callout block whose content
#     should survive as plain text/paragraph, no wrapper.
# Both are literal leaked HTML wrappers, so the general span/div-unwrap
# transform (see ``_render_node``) already repairs a WELL-FORMED instance of
# either one; this module only needs the same two regexes to (a) label a
# successful unwrap of one of these specifically as ``R_ARTIFACT`` rather than
# the generic ``R_HTML_INLINE``, and (b) recognize a MALFORMED instance (no
# matching close tag — an actually-corrupted remnant) so it is never guessed
# at, only reported.
_ARTIFACT_SPAN_RE = re.compile(r'<span class="?citation')
_ARTIFACT_DIV_RE = re.compile(r'<div class="?notice')

# --- hygiene: zero-width / bidi control characters ---------------------------

# Exact code points removed outside code (all zero-width and Unicode
# bidi-control formatting characters that have no business in plain prose):
#   U+200B ZERO WIDTH SPACE           U+200C ZERO WIDTH NON-JOINER
#   U+200D ZERO WIDTH JOINER          U+200E LEFT-TO-RIGHT MARK
#   U+200F RIGHT-TO-LEFT MARK         U+2060 WORD JOINER
#   U+FEFF ZERO WIDTH NO-BREAK SPACE (BOM)
#   U+202A-U+202E (LRE/RLE/PDF/LRO/RLO — explicit bidi embedding/override)
#   U+2066-U+2069 (LRI/RLI/FSI/PDI — bidi isolates)
_ZERO_WIDTH_BIDI_CHARS = "​‌‍‎‏⁠﻿‪‫‬‭‮⁦⁧⁨⁩"
_CONTROL_CHAR_RE = re.compile("[" + _ZERO_WIDTH_BIDI_CHARS + "]")

_NBSP = " "

_BLANK_RUN_RE = re.compile(r"\n{3,}")

_TAG_SCAN_RE = re.compile(
    r"<(?P<close>/)?(?P<name>[a-zA-Z][a-zA-Z0-9]*)(?P<attrs>(?:\s[^<>]*)?)(?P<selfclose>/)?>"
)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-\w:.]*)\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+)""")


def _parse_attrs(attr_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_text):
        name = m.group(1).lower()
        raw = m.group(2)
        if raw and raw[0] in "\"'":
            raw = raw[1:-1]
        attrs[name] = html.unescape(raw)
    return attrs


# --- block-structure-preserving escapes --------------------------------
#
# Several edits (file-link/bare-link unwrap, a same-region span/div unwrap,
# an inline corruption-artifact repair) can leave NEW text sitting at the
# start of a line that previously held something else — e.g. a whole
# standalone paragraph ``[9. Appendix A](file:///...#_Toc9)`` unwraps to
# ``9. Appendix A``, which CommonMark then reads as the START of an ORDERED
# LIST, silently turning a paragraph into a list. ``_line_block_kind``
# classifies what block construct a line would start; ``_escape_block_marker``
# backslash-escapes that marker (CommonMark only treats a backslash before
# ASCII punctuation as an escape, so the escaped form renders identically to
# the unescaped one — no visible text changes) so the block type an edit
# didn't intend to change, doesn't. The one deliberate exception (a
# cross-block corruption-artifact div whose closing tag was glued to a
# heading that was always MEANT to be a heading) is never routed through this
# guard — see ``_strip_cross_region_wrappers``.
_INDENT_RE = re.compile(r"^ {0,3}")
_ATX_MARK_RE = re.compile(r"#{1,6}(?=[ \t]|$)")
_BULLET_MARK_RE = re.compile(r"[-*+](?=[ \t]|$)")
_ORDERED_MARK_RE = re.compile(r"\d{1,9}[.)](?=[ \t]|$)")
_THEMATIC_BREAK_MARK_RE = re.compile(r"(-[ \t]*){3,}|(\*[ \t]*){3,}|(_[ \t]*){3,}")
_SETEXT_MARK_RE = re.compile(r"(=+|-{2,})[ \t]*")
_FENCE_MARK_RE = re.compile(r"`{3,}|~{3,}")
_INDENTED_CODE_RE = re.compile(r"^ {4,}\S")


def _line_block_kind(line: str) -> str | None:
    """A deliberately conservative (not full-CommonMark-precision) heuristic
    classification of what block construct ``line`` would start if it were
    encountered fresh in a document — ``None`` means ordinary paragraph/
    continuation text, no special marker."""
    if _INDENTED_CODE_RE.match(line):
        return "indented-code"
    indent = _INDENT_RE.match(line)
    rest = line[indent.end() :] if indent else line
    if _FENCE_MARK_RE.match(rest):
        return "fence"
    if _THEMATIC_BREAK_MARK_RE.fullmatch(rest):
        return "thematic-break"
    if _ATX_MARK_RE.match(rest):
        return "atx-heading"
    if _SETEXT_MARK_RE.fullmatch(rest):
        return "setext-underline"
    if rest.startswith(">"):
        return "blockquote"
    if _ORDERED_MARK_RE.match(rest):
        return "ordered-list"
    if _BULLET_MARK_RE.match(rest):
        return "bullet-list"
    m = _TAG_SCAN_RE.match(rest)
    if m and m.group("name").lower() in _KNOWN_HTML_TAGS:
        return "html-block-start"
    return None


def _escape_block_marker(line: str) -> str:
    """Backslash-escape whatever ``_line_block_kind`` detected so ``line``
    renders identically but no longer starts that block construct.
    ``indented-code`` is left alone: none of this module's edits ever ADD
    leading whitespace, only remove/replace characters, so it can't actually
    be produced by an edit — there is no backslash escape for it either way."""
    kind = _line_block_kind(line)
    if kind is None or kind == "indented-code":
        return line
    indent = _INDENT_RE.match(line)
    i = indent.end() if indent else 0
    if kind == "ordered-list":
        while i < len(line) and line[i].isdigit():
            i += 1
        return line[:i] + "\\" + line[i:]
    return line[:i] + "\\" + line[i:]


def _guard_block_type_drift(old_text: str, new_text: str) -> str:
    """Re-escape any line of ``new_text`` whose leading block-marker
    classification differs from the SAME line of ``old_text`` — only checked
    when both have the same line count (the common case for a link/span/
    artifact edit, which only ever removes or replaces text within existing
    lines; a replacement that also changes the line count, e.g. converting an
    HTML ``<br>`` into a hard break, is left to this module's other
    guarantees rather than guessed at here)."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    if len(old_lines) != len(new_lines):
        return new_text
    changed = False
    out = []
    for old, new in zip(old_lines, new_lines, strict=True):
        new_kind = _line_block_kind(new)
        if new_kind is not None and new_kind != _line_block_kind(old):
            out.append(_escape_block_marker(new))
            changed = True
        else:
            out.append(new)
    return "\n".join(out) if changed else new_text


# --- a permissive HTML-fragment tree, immune to the placeholder trap --------


@dataclass
class _HNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_HNode | str] = field(default_factory=list)


def _parse_fragment(text: str) -> tuple[_HNode, bool]:
    """Parse ``text`` into a tiny tree. A tag-shaped token whose name is NOT a
    known HTML element (see module docstring) is never structural — it is kept
    as literal text exactly as written. Returns ``(root, ok)``; ``ok`` is
    False if a KNOWN tag was mismatched/unclosed, meaning the tree cannot be
    trusted for conversion (caller must leave the original text untouched)."""
    root = _HNode("root")
    stack = [root]
    ok = True
    pos = 0
    for m in _TAG_SCAN_RE.finditer(text):
        if m.start() > pos:
            stack[-1].children.append(text[pos : m.start()])
        name = m.group("name").lower()
        if name not in _KNOWN_HTML_TAGS:
            stack[-1].children.append(m.group(0))
            pos = m.end()
            continue
        if m.group("close"):
            if len(stack) > 1 and stack[-1].tag == name:
                stack.pop()
            else:
                ok = False
                stack[-1].children.append(m.group(0))
            pos = m.end()
            continue
        node = _HNode(name, _parse_attrs(m.group("attrs") or ""))
        stack[-1].children.append(node)
        if not (m.group("selfclose") or name in _VOID_TAGS):
            stack.append(node)
        pos = m.end()
    if pos < len(text):
        stack[-1].children.append(text[pos:])
    if len(stack) != 1:
        ok = False
    return root, ok


def _node_text(node: _HNode | str) -> str:
    """Flatten a node to its plain visible text, ignoring all tags."""
    if isinstance(node, str):
        return node
    return "".join(_node_text(c) for c in node.children)


_ALLOWED_LINK_SCHEMES = frozenset({"", "http", "https", "mailto", "file"})


# --- locating links/images via markdown-it's own tokenizer -----------------
#
# See the ``_MD.validateLink`` override (top-level orchestration section
# below): this module never relies on a regex to find ``[text](url)``/
# ``![alt](url)`` — CommonMark link text allows balanced nested brackets
# (e.g. a footnote-citation shape ``[[9]](file:///...#_ftn9)``, whose TEXT
# is the literal string ``[9]``, which a ``[^\]]*``-style regex can't parse),
# and reference-style links (``[text][ref]``, shortcut ``[ref]``) and
# autolinks (``<file:///...>``) have no ``(url)`` in their own syntax for a
# regex to anchor on at all. markdown-it's real inline tokenizer parses all
# of these correctly; this module only needs to recover each token's SOURCE
# SPAN (markdown-it-py doesn't track inline positions — see the module
# docstring) and which syntax form it was, since content that needs no
# repair (an allowed-scheme link) must come back byte-identical rather than
# reconstructed from the URL markdown-it hands back already normalized
# (e.g. a raw space in the path becomes ``%20``).


@dataclass(frozen=True)
class _LinkSpan:
    start: int
    end: int
    is_image: bool
    href: str
    title: str | None
    text: str  # link text, or an image's alt text
    syntax: str  # "bracket" | "reference" | "shortcut" | "autolink"


def _find_matching_paren(text: str, open_pos: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``open_pos``, honoring
    backslash-escapes and balanced nested parens (a URL can legitimately
    contain them, e.g. a Wikipedia-style path)."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _find_matching_bracket(text: str, open_pos: int) -> int | None:
    """Index of the ``]`` matching the ``[`` at ``open_pos``, honoring
    backslash-escapes and balanced nested brackets — CommonMark link text
    may itself contain a literal bracketed span (the double-bracket
    footnote-citation shape, ``[[9]](url)``) or the SAME shape wrapped in
    emphasis/strong markup (``[**[9]**](url)``, seen on real pages), whose
    joined leaf ``.content`` alone (empty for the ``strong_open``/``_close``
    tokens themselves) can no longer be turned back into the original
    bracket span by reconstructing ``"[" + text + "]"`` — see
    ``_locate_link_span``, which uses this instead of that reconstruction."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _locate_link_span(
    text: str, cursor: int, *, is_image: bool, is_autolink: bool
) -> tuple[int, int, str] | None:
    """Find the ``(start, end, syntax)`` span (``end`` exclusive) of one
    link/image construct in ``text`` at or after ``cursor`` — since link_
    open/link_close/image tokens carry no raw span of their own. Locates the
    construct's own opening ``[``/``![`` (the first one at or after
    ``cursor`` — always the right one, since ``cursor`` has already
    advanced past every OTHER child's own content in source order by the
    time a link/image is reached, see ``_find_link_spans``) and its
    matching ``]`` via ``_find_matching_bracket`` — never by reconstructing
    ``"[" + text + "]"`` from the construct's own resolved text, which is
    only byte-identical to the source when the link text has no nested
    markup at all."""
    if is_autolink:
        start = text.find("<", cursor)
        if start == -1:
            return None
        end = text.find(">", start)
        if end == -1:
            return None
        return start, end + 1, "autolink"
    start = text.find("![" if is_image else "[", cursor)
    if start == -1:
        return None
    bracket_start = start + 1 if is_image else start
    close = _find_matching_bracket(text, bracket_start)
    if close is None:
        return None
    pos = close + 1
    if pos < len(text) and text[pos] == "(":
        close_paren = _find_matching_paren(text, pos)
        if close_paren is None:
            return None
        return start, close_paren + 1, "bracket"
    if pos < len(text) and text[pos] == "[":
        close_ref = text.find("]", pos)
        if close_ref == -1:
            return None
        return start, close_ref + 1, "reference"
    return start, pos, "shortcut"


def _find_link_spans(text: str, env: dict[str, object]) -> list[_LinkSpan]:
    """Every link/image construct in ``text`` — bracket, reference-style,
    shortcut-reference, or autolink — located via markdown-it's real inline
    tokenizer. ``env`` must be the SAME dict a full-document parse populated
    (its ``"references"`` entry is what lets a reference-style/shortcut link
    resolve even when ``text`` is just one paragraph's own content, not the
    whole document — markdown-it's reference rule is a block-level, whole-
    document pass — see ``_convert_structural``/``visible_text``, which each
    parse the whole document once and pass the resulting ``env`` down to
    every region/leaf they process). A construct this module can't
    locate this way (should not happen in practice) is simply omitted —
    callers that need to know "is there something actionable here at all"
    use their own token/regex scan (``_looks_actionable``), so nothing is
    silently dropped from the user-facing accounting even if a span can't be
    edited."""
    try:
        tokens = _MD.parseInline(text, env)
    except Exception:  # pragma: no cover - defensive, see _convert_structural
        return []
    if not tokens:
        return []
    children = tokens[0].children or []
    spans: list[_LinkSpan] = []
    cursor = 0
    i = 0
    n = len(children)
    while i < n:
        child = children[i]
        ctype = child.type
        if ctype == "link_open":
            inner_parts: list[str] = []
            j = i + 1
            while j < n and children[j].type != "link_close":
                inner_parts.append(getattr(children[j], "content", "") or "")
                j += 1
            link_text = "".join(inner_parts)
            attrs = dict(child.attrs) if child.attrs else {}
            href = str(attrs.get("href", ""))
            title = attrs.get("title")
            is_autolink = child.markup == "autolink"
            located = _locate_link_span(text, cursor, is_image=False, is_autolink=is_autolink)
            if located is not None:
                start, end, syntax = located
                spans.append(
                    _LinkSpan(
                        start,
                        end,
                        False,
                        href,
                        str(title) if title is not None else None,
                        link_text,
                        syntax,
                    )
                )
                cursor = end
            i = j + 1
            continue
        if ctype == "image":
            attrs = dict(child.attrs) if child.attrs else {}
            src = str(attrs.get("src", ""))
            alt = getattr(child, "content", "") or ""
            located = _locate_link_span(text, cursor, is_image=True, is_autolink=False)
            if located is not None:
                start, end, syntax = located
                spans.append(_LinkSpan(start, end, True, src, None, alt, syntax))
                cursor = end
            i += 1
            continue
        content = getattr(child, "content", "") or ""
        if content:
            idx = text.find(content, cursor)
            if idx != -1:
                cursor = idx + len(content)
        i += 1
    return spans


def _text_leaf_unsupported_reason(text: str, env: dict[str, object]) -> str | None:
    """A native markdown construct in plain text with NO safe fallback at
    all — only a ``file:`` AUTOLINK, which has no separate text to keep once
    its target is dropped (unlike a bracket-style link, which can always
    fall back to keeping its own text — see ``_process_link_target``), so it
    is never guessed at and blocks conversion of its WHOLE containing text.
    Every OTHER scheme this module doesn't know how to repair (not
    ``file:``/bare, which ``_process_link_target`` always repairs, and not
    http(s)/mailto, which are already fine) is deliberately NON-blocking: it
    is left exactly as originally written by ``_render_link_span``, which
    also records why — see ``_RenderCtx.unresolved_notes`` — so a genuinely
    unrelated ``file:`` link a few words later in the SAME paragraph still
    gets stripped rather than the whole paragraph being left untouched."""
    for span in _find_link_spans(text, env):
        if span.syntax == "autolink" and urlsplit(span.href).scheme.lower() == "file":
            return f"autolink to a file: URL has no text to keep on its own: <{span.href}>"
    return None


def _has_unsupported(node: _HNode | str, env: dict[str, object]) -> str | None:
    """Depth-first search for something this module refuses to convert or
    repair — an unknown-but-real HTML tag, a table with colspan/rowspan/
    nesting, or a link scheme it won't guess at. Returns a human-readable
    reason, or None if the whole subtree is convertible."""
    if isinstance(node, str):
        return _text_leaf_unsupported_reason(node, env)
    tag = node.tag
    if tag == "table":
        return _table_unsupported_reason(node, env)
    if tag == "a":
        href = node.attrs.get("href", "")
        if href.strip() not in ("", "/") and urlsplit(href).scheme.lower() not in (
            _ALLOWED_LINK_SCHEMES
        ):
            return f"<a href> with unsupported link scheme: {href!r}"
    elif tag not in _INLINE_CONVERTIBLE | {"p", "div"}:
        return f"unsupported HTML tag <{tag}>"
    for c in node.children:
        reason = _has_unsupported(c, env)
        if reason:
            return reason
    return None


def _table_unsupported_reason(table: _HNode, env: dict[str, object]) -> str | None:
    for cell in _iter_table_cells(table):
        if "colspan" in cell.attrs or "rowspan" in cell.attrs:
            return "table cell uses colspan/rowspan"
        for c in cell.children:
            if _contains_tag(c, "table"):
                return "nested table"
            reason = _has_unsupported(c, env)
            if reason:
                return reason
    return None


def _contains_tag(node: _HNode | str, tag: str) -> bool:
    if isinstance(node, str):
        return False
    if node.tag == tag:
        return True
    return any(_contains_tag(c, tag) for c in node.children)


def _iter_table_cells(table: _HNode) -> Iterable[_HNode]:
    for child in table.children:
        if isinstance(child, str):
            continue
        if child.tag in ("th", "td"):
            yield child
        elif child.tag in ("thead", "tbody", "tfoot", "tr"):
            yield from _iter_table_cells(child)


# --- inline rendering (span/div/sup/br/strong/b/em/i/code/a) ---------------

_INLINE_CONVERTIBLE = frozenset({"span", "sup", "br", "strong", "b", "em", "i", "code", "a", "img"})

_FOOTNOTE_MARKER_RE = re.compile(r"^\s*[\[\(]?[0-9*†‡§]{1,3}[\]\)]?\s*$")


def _wrap(marker: str, content: str) -> str:
    if content.strip() == "":
        return content
    lead = content[: len(content) - len(content.lstrip())]
    trail = content[len(content.rstrip()) :]
    core = content.strip()
    return f"{lead}{marker}{core}{marker}{trail}"


@dataclass
class _RenderCtx:
    own_hosts: frozenset[str]
    image_map: Mapping[str, str]
    #: The workspace's own BookStack host (``host[:port]``, no scheme) — lets a
    #: HOST-RELATIVE gallery reference (``/uploads/images/gallery/...``, no
    #: scheme/host at all — real BookStack exports use this spelling as often
    #: as the fully-qualified one) be turned into the same canonical absolute
    #: URL an ``own_hosts``-qualified absolute reference already resolves to,
    #: so both spellings key into ``image_map``/``CleanResult.images``
    #: identically (see ``_canonical_gallery_url``). ``None`` leaves a
    #: host-relative reference unrecognized (today's behavior — see
    #: ``clean_page``'s docstring for the caller-facing knob, ``bs_host``).
    bs_host: str | None = None
    #: Whether the page being cleaned sits inside a chapter — REF-007's two
    #: accepted local spellings for a gallery image reference are
    #: ``images/<file>`` at the book root and ``../images/<file>`` one level
    #: down inside a chapter; ``clean_page`` always emitted the book-root
    #: spelling regardless of where the page actually lives (a real REF-007
    #: violation for any chapter page whose gallery image needed rewriting).
    in_chapter: bool = False
    env: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    # A specific link/autolink with a scheme this module refuses to guess at
    # (not file:, which is always repaired, and not http(s)/mailto, which
    # are always fine) is left exactly as originally written — see
    # _render_link_span — but still worth a human's attention: recorded here
    # rather than blocking conversion of the REST of its containing text (an
    # unrelated file: link a few words later in the same paragraph must
    # still be stripped; see the module's "zero file: destinations"
    # requirement and this field's one caller in _convert_structural).
    unresolved_notes: list[str] = field(default_factory=list)


def _escape_brackets(text: str) -> str:
    """Backslash-escape literal ``[``/``]`` characters so text that used to
    be a link's own bracketed content (e.g. the footnote-citation shape
    ``[[9]](file:///...)``, whose text is literally ``[9]``) can never pair
    with a following ``(...)`` or a reference definition and accidentally
    form a NEW link once the original one is stripped down to plain text —
    renders identically (CommonMark's only escape form), never guessed at."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _process_link_target(text: str, href: str) -> tuple[str, str | None]:
    """Apply the D5 link rules to one ``(link text, href)`` pair. Returns
    ``(rendered, rule)`` where ``rule`` is the Change rule name if something
    was rewritten, or None if the link needs no target rewrite — either
    because ``[text](href)`` is already fine (http(s)/mailto), or because
    its scheme is one this module refuses to guess at, in which case the
    caller (``_render_link_span``) leaves the ORIGINAL syntax untouched
    (never this function's reconstructed ``rendered``) and records why."""
    scheme = urlsplit(href).scheme.lower()
    if scheme == "file":
        return _escape_brackets(text), R_FILE_LINK
    if href.strip() in ("", "/"):
        return _escape_brackets(text), R_BARE_LINK
    return f"[{text}]({href})", None


def _render_children(node: _HNode, ctx: _RenderCtx) -> str:
    return "".join(_render_node(c, ctx) for c in node.children)


def _render_node(node: _HNode | str, ctx: _RenderCtx) -> str:
    if isinstance(node, str):
        return _render_text_leaf(node, ctx)
    tag = node.tag
    if tag in ("span", "div"):
        cls = node.attrs.get("class", "")
        inner = _render_children(node, ctx)
        artifact_re = _ARTIFACT_SPAN_RE if tag == "span" else _ARTIFACT_DIV_RE
        if artifact_re.search(f'<{tag} class="{cls}'):
            ctx.notes.append(R_ARTIFACT)
        else:
            ctx.notes.append(R_HTML_INLINE)
        return inner
    if tag == "sup":
        inner = _render_children(node, ctx)
        ctx.notes.append(R_HTML_INLINE)
        if _FOOTNOTE_MARKER_RE.match(inner):
            return f"({inner.strip()})"
        return inner
    if tag == "br":
        ctx.notes.append(R_HTML_INLINE)
        return "\\\n"
    if tag in ("strong", "b"):
        ctx.notes.append(R_HTML_INLINE)
        return _wrap("**", _render_children(node, ctx))
    if tag in ("em", "i"):
        ctx.notes.append(R_HTML_INLINE)
        return _wrap("*", _render_children(node, ctx))
    if tag == "code":
        ctx.notes.append(R_HTML_INLINE)
        text = _node_text(node)
        fence = "`" * (max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{fence}{pad}{text}{pad}{fence}" if text else ""
    if tag == "a":
        href = node.attrs.get("href", "")
        inner = _render_children(node, ctx)
        rendered, rule = _process_link_target(inner, href)
        ctx.notes.append(rule or R_HTML_INLINE)
        return rendered
    if tag == "img":
        alt = node.attrs.get("alt", "")
        src = node.attrs.get("src", "")
        return _render_image(alt, src, ctx, orig=None)
    # p / other block wrappers reachable only from block-level conversion
    return _render_children(node, ctx)


def _canonical_gallery_url(src: str, ctx: _RenderCtx) -> str | None:
    """``src`` -> the canonical absolute gallery URL it names, if it names one
    of THIS wiki's own ``/uploads/images/...`` gallery uploads at all — either
    spelling a real BookStack export uses (module docstring/BRIEF item 4a):
    a fully-qualified ``https://<own-host>/uploads/images/...`` reference, or
    a host-relative ``/uploads/images/...`` one (no scheme/host). Both key
    into ``ctx.image_map``/``CleanResult.images`` under the SAME absolute URL
    either way, so a page mixing both spellings for the same file (seen on
    real pages) still gets ONE proposed local filename, not two. Returns
    ``None`` for anything else, including a host-relative reference when
    ``ctx.bs_host`` isn't known (nothing to build an absolute URL from)."""
    if src.startswith("/uploads/images/"):
        return f"https://{ctx.bs_host}{src}" if ctx.bs_host else None
    if (
        src.startswith(("http://", "https://"))
        and urlsplit(src).netloc in ctx.own_hosts
        and "/uploads/images/" in src
    ):
        return src
    return None


def _render_image(alt: str, src: str, ctx: _RenderCtx, *, orig: str | None) -> str:
    """Render one image. ``orig`` is the exact original markdown source span
    (``!alt-text ... `` slice) when this came from ALREADY-native markdown
    image syntax — returned byte-identical when nothing needs to change,
    rather than reconstructed from ``src`` (which, for a link/image found
    via markdown-it's tokenizer, may already be URL-normalized, e.g. a raw
    space encoded to ``%20`` — reconstructing would silently rewrite content
    that was never in violation of anything). ``orig=None`` for an ``<img>``
    HTML tag being converted, which always needs reconstruction since the
    syntax itself is changing from HTML to markdown."""
    scheme = urlsplit(src).scheme.lower()
    canonical = _canonical_gallery_url(src, ctx)
    if canonical is not None:
        rel_prefix = "../images/" if ctx.in_chapter else "images/"  # REF-007
        local = ctx.image_map.get(canonical)
        if local is not None:
            ctx.notes.append(R_GALLERY_IMAGE)
            return f"![{alt}]({rel_prefix}{local})"
        filename = url2pathname(urlsplit(canonical).path.rsplit("/", 1)[-1])
        ctx.images.append(ImageRef(url=canonical, proposed_filename=filename))
        if orig is not None:
            return orig  # nothing textually changes yet, just recorded above
        ctx.notes.append(R_HTML_INLINE)
        return f"![{alt}]({canonical})"
    if scheme == "file":
        ctx.notes.append(R_FILE_LINK)
        return _escape_brackets(alt)  # D5: a file: image keeps only its alt text
    if orig is not None:
        return orig
    ctx.notes.append(R_HTML_INLINE)
    return f"![{alt}]({src})"


def _render_text_leaf(text: str, ctx: _RenderCtx) -> str:
    """Apply link-scheme handling and gallery-image rewriting to native
    markdown syntax sitting inside plain text (covers both prose that was
    never HTML at all, and text left over between converted tags) — located
    via markdown-it's own tokenizer (``_find_link_spans``), never a regex
    (see that function's docstring for why)."""
    spans = _find_link_spans(text, ctx.env)
    if not spans:
        return text
    out = []
    cursor = 0
    for span in spans:
        out.append(text[cursor : span.start])
        out.append(_render_link_span(span, text, ctx))
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)


def _render_link_span(span: _LinkSpan, source: str, ctx: _RenderCtx) -> str:
    orig = source[span.start : span.end]
    if span.is_image:
        return _render_image(span.text, span.href, ctx, orig=orig)
    scheme = urlsplit(span.href).scheme.lower()
    unsupported_scheme = span.href.strip() not in ("", "/") and scheme not in _ALLOWED_LINK_SCHEMES
    if span.syntax == "autolink":
        # A file: autolink is gated out earlier as hard-unsupported (no text
        # to keep at all — see _text_leaf_unsupported_reason). Any OTHER
        # unsupported scheme has a safe fallback: leave it untouched exactly
        # as written, never reconstructed into bracket syntax, and note WHY
        # for a human to review — it doesn't block the REST of this text.
        if unsupported_scheme:
            ctx.unresolved_notes.append(f"autolink with unsupported scheme left as-is: {orig}")
        return orig
    rendered, rule = _process_link_target(span.text, span.href)
    if rule is None:
        # nothing to fix: leave the ORIGINAL syntax exactly as written,
        # whether that was `[text](url)`, a reference usage, or a shortcut
        # — reconstructing risks silently rewriting a URL markdown-it has
        # already normalized (see _render_image's docstring, same reasoning).
        if unsupported_scheme:
            ctx.unresolved_notes.append(
                f"link with unsupported scheme left as-is: [{span.text}]({span.href})"
            )
        return orig
    ctx.notes.append(rule)
    return rendered


# --- table conversion ---------------------------------------------------


def _cell_text(cell: _HNode, ctx: _RenderCtx) -> str:
    parts = []
    for child in cell.children:
        if isinstance(child, _HNode) and child.tag == "p":
            parts.append(_render_children(child, ctx).strip())
        else:
            parts.append(_render_node(child, ctx))
    joined = " ".join(p for p in parts if p.strip() != "")
    joined = re.sub(r"\s*\n\s*", " ", joined).strip()
    return joined.replace("|", "\\|")


def _table_rows(table: _HNode) -> tuple[list[_HNode] | None, list[list[_HNode]]]:
    """Returns ``(header_row_cells_or_None, body_rows)`` — a ``<thead>``'s row
    (its ``<th>``/``<td>`` cells) if present, else None (caller infers)."""
    header: list[_HNode] | None = None
    body: list[list[_HNode]] = []

    def row_cells(tr: _HNode) -> list[_HNode]:
        return [c for c in tr.children if isinstance(c, _HNode) and c.tag in ("th", "td")]

    def collect(container: _HNode, into: list[list[_HNode]]) -> None:
        for child in container.children:
            if isinstance(child, _HNode) and child.tag == "tr":
                into.append(row_cells(child))

    thead = next((c for c in table.children if isinstance(c, _HNode) and c.tag == "thead"), None)
    if thead is not None:
        rows: list[list[_HNode]] = []
        collect(thead, rows)
        if rows:
            header = rows[0]
            body.extend(rows[1:])
    for child in table.children:
        if isinstance(child, _HNode) and child.tag in ("tbody", "tfoot"):
            collect(child, body)
        elif isinstance(child, _HNode) and child.tag == "tr" and thead is None:
            body.append(row_cells(child))
    return header, body


def _table_to_markdown(table: _HNode, ctx: _RenderCtx) -> tuple[str, bool]:
    """Returns ``(markdown, header_was_inferred)``. Caller has already
    confirmed via ``_table_unsupported_reason`` that this table has no
    colspan/rowspan/nesting."""
    header, body = _table_rows(table)
    inferred = False
    if header is None:
        if body and any(c.tag == "th" for c in body[0]):
            header, body = body[0], body[1:]
        elif body:
            header, body = body[0], body[1:]
            inferred = True
        else:
            header = []
    ncols = max([len(header), *(len(r) for r in body)], default=0)

    def row_line(cells: list[_HNode]) -> str:
        texts = [_cell_text(c, ctx) for c in cells]
        texts += [""] * (ncols - len(texts))
        return "| " + " | ".join(texts) + " |"

    lines = [row_line(header), "| " + " | ".join(["---"] * ncols) + " |"]
    lines.extend(row_line(r) for r in body)
    return "\n".join(lines), inferred


# --- block-level (html_block) conversion -------------------------------


def _convert_block_fragment(raw: str, ctx: _RenderCtx) -> tuple[str | None, str | None]:
    """Convert one ``html_block`` token's raw text to markdown. Returns
    ``(None, reason)`` if any part of it can't be safely converted — the
    caller leaves the WHOLE block untouched and reports ``reason`` as one
    Issue (see module docstring: no hybrid half-HTML output)."""
    root, ok = _parse_fragment(raw)
    if not ok:
        return None, "malformed or mismatched HTML markup"
    for child in root.children:
        reason = _has_unsupported(child, ctx.env)
        if reason:
            return None, reason
    out: list[str] = []
    for child in root.children:
        if isinstance(child, str):
            if child.strip():
                out.append(child.strip())
            continue
        if child.tag == "table":
            md_table, inferred = _table_to_markdown(child, ctx)
            ctx.notes.append(R_HTML_TABLE)
            if inferred:
                ctx.notes.append("header-row-inferred")
            out.append(md_table)
        elif child.tag == "p":
            rendered = _render_children(child, ctx).strip()
            ctx.notes.append(R_HTML_INLINE)
            if rendered:
                out.append(rendered)
        else:
            rendered = _render_node(child, ctx).strip()
            if rendered:
                out.append(rendered)
    return "\n\n".join(out), None


# --- top-level orchestration ---------------------------------------------

_MD = MarkdownIt("commonmark")
# markdown-it-py's default validateLink REJECTS file:/javascript:/vbscript:/
# data: destinations, which means a `file:` link or image never becomes a
# link_open/image token at all — it stays literal, un-tokenized text, and a
# link whose TEXT itself is bracketed (``[[9]](file:///...#_ftn9)``, a common
# footnote-citation shape) becomes impossible to find with a regex too, since
# CommonMark link text allows balanced nested brackets. Overridden here to
# accept every scheme so LOCATING a link/image is never scheme-dependent —
# this module's OWN scheme policy (allow http(s)/mailto, repair file:/bare,
# refuse anything else) is enforced afterward, in _find_link_spans's callers,
# never by markdown-it. This only affects INLINE link/image/autolink
# tokenization (confirmed against markdown-it-py's rule set — block-level
# parsing, which is all the structural guard and hygiene passes need, never
# consults validateLink), so it is safe to apply on this one shared instance.
_MD.validateLink = lambda url: True  # type: ignore[method-assign]


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _excerpt(s: str, n: int = 80) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _line_index_at(offsets: list[int], pos: int) -> int:
    """0-indexed line number containing absolute offset ``pos``."""
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _apply_edits(
    text: str, edits: Sequence[tuple[int, int, str]]
) -> tuple[str, list[tuple[int, int]]]:
    """Apply (start, end, new_text) edits to ``text`` (need not be
    pre-sorted; must be non-overlapping). Returns the new text and, for each
    edit IN SORTED (applied) ORDER, the (start, end) span its replacement
    occupies in the NEW text — the one primitive every pipeline stage uses
    both to edit text and to carry a caller-supplied set of "this span is
    deliberately-converted content" ranges forward into the next stage (see
    ``_shift_ranges`` and the module docstring's final structural guard). A
    caller that needs to associate a span back to its own per-edit metadata
    (e.g. "was this replacement deliberate?") must sort that metadata with
    the same ``key=lambda e: e[0]`` first."""
    ordered = sorted(edits, key=lambda e: e[0])
    out: list[str] = []
    cursor = 0
    new_pos = 0
    spans: list[tuple[int, int]] = []
    for a, b, new in ordered:
        if a < cursor:
            spans.append((new_pos, new_pos))
            continue
        out.append(text[cursor:a])
        new_pos += a - cursor
        out.append(new)
        spans.append((new_pos, new_pos + len(new)))
        new_pos += len(new)
        cursor = b
    out.append(text[cursor:])
    return "".join(out), spans


def _shift_ranges(
    ranges: list[tuple[int, int]], edits: Sequence[tuple[int, int, str]]
) -> list[tuple[int, int]]:
    """Recompute each ``(start, end)`` in ``ranges`` (coordinates of the text
    BEFORE ``edits``) to its position in the text AFTER applying ``edits``.
    An edit strictly before a range shifts it by the edit's own length delta;
    an edit strictly after a range doesn't affect it; an edit that overlaps a
    range's boundary conservatively WIDENS the range to still cover it,
    rather than risk silently losing track of a deliberately-converted span
    (this module's own edits never actually straddle a tracked range's
    boundary — deliberate ranges are always whole lines and later edits
    never cross a line boundary — so this is a defensive fallback, not the
    normal path)."""
    ordered = sorted(edits, key=lambda e: e[0])
    result: list[tuple[int, int]] = []
    for r_start, r_end in ranges:
        new_start, new_end = r_start, r_end
        shift = 0
        for a, b, new in ordered:
            delta = len(new) - (b - a)
            if b <= r_start:
                shift += delta
            elif a >= r_end:
                continue
            else:
                new_start = min(new_start, a)
                new_end = max(new_end, b) + delta
        new_start += shift
        new_end += shift
        if new_end > new_start:
            result.append((new_start, new_end))
    return result


def _remap_lines(
    line_map: list[int | None], edits: Sequence[tuple[int, int, str]]
) -> list[int | None]:
    """The line-map primitive the final structural guard is built on
    (replacing the old sequence-alignment-with-exclusion-ranges approach,
    which cascaded a single unrepairable divergence into discarding every
    OTHER, unrelated escape and into blaming the wrong line — see the
    module's final-guard docstring). ``line_map`` holds, for each ORIGINAL
    line (by index), the char offset where that line currently starts in
    whatever text ``edits`` is about to be applied to (or ``None`` if that
    original line no longer has a distinct position at all). Returns the
    same list updated to point into the text AFTER ``edits``.

    An entry whose offset sits exactly at some edit's own start "becomes"
    wherever that edit's replacement now begins (this is what makes a
    same-line edit — the overwhelming majority: stripping a tag or a link
    inline, mid-line — a no-op for the entry that happens to sit at the very
    start of its line, and what makes the FIRST line of a multi-line
    html_block conversion correctly track the start of its replacement).
    Every other entry whose offset falls STRICTLY inside an edit's replaced
    span (a line fully consumed by a multi-line replacement, e.g. the 2nd+
    line of a converted html_block, or a line deleted outright) becomes
    ``None`` — it no longer has a distinct identity to compare against."""
    if not edits or not line_map:
        return line_map
    ordered = sorted(edits, key=lambda e: e[0])
    starts = [e[0] for e in ordered]
    ends = [e[1] for e in ordered]
    cum_shifts: list[int] = []
    cum = 0
    for a, b, new in ordered:
        cum += len(new) - (b - a)
        cum_shifts.append(cum)

    def shift_of(pos: int) -> int:
        i = bisect.bisect_right(ends, pos)
        return cum_shifts[i - 1] if i > 0 else 0

    def find_containing(pos: int) -> int | None:
        i = bisect.bisect_right(starts, pos) - 1
        if i >= 0 and starts[i] <= pos < ends[i]:
            return i
        return None

    new_map: list[int | None] = []
    for cur in line_map:
        if cur is None:
            new_map.append(None)
            continue
        ei = find_containing(cur)
        if ei is None:
            new_map.append(cur + shift_of(cur))
            continue
        a = starts[ei]
        if cur == a:
            new_map.append(a + shift_of(a))
        else:
            new_map.append(None)
    return new_map


# --- cross-block wrapper pairing (a leaked ``<div>``/``<span>`` whose closing
# tag landed in a LATER html_block, after a blank line severed it from its
# opener — the Word/TinyMCE export corruption D5 exists to repair) ----------

_WRAPPER_PAIR_TAGS = frozenset({"div", "span"})


@dataclass(frozen=True)
class _TagEvent:
    start: int
    end: int
    name: str
    is_close: bool
    region: int
    region_lines: tuple[int, int]  # [start_line, end_line) of the WHOLE region


def _find_div_span_events(text: str, tokens: Sequence[object]) -> list[_TagEvent]:
    """Every ``<div>``/``<span>`` open or close tag in the document, in
    source order, tagged with which "region" (one html_block, or one
    locatable inline block) it was found in — the raw material for pairing
    openers and closers across region boundaries. Code spans are skipped, per
    the same ``_code_mask`` machinery the rest of this module already uses."""
    offsets = _line_offsets(text)
    n_lines = len(text.split("\n"))

    def slice_lines(start: int, end: int) -> tuple[int, int]:
        a = offsets[start]
        b = offsets[end] if end < len(offsets) else len(text)
        return a, b

    events: list[_TagEvent] = []
    region = 0

    def scan(
        raw: str, base: int, mask: list[tuple[int, int]], region_lines: tuple[int, int]
    ) -> None:
        nonlocal region
        for m in _TAG_SCAN_RE.finditer(raw):
            name = m.group("name").lower()
            if name not in _WRAPPER_PAIR_TAGS:
                continue
            if mask and any(a <= m.start() < b for a, b in mask):
                continue
            events.append(
                _TagEvent(
                    base + m.start(),
                    base + m.end(),
                    name,
                    bool(m.group("close")),
                    region,
                    region_lines,
                )
            )
        region += 1

    for tok in tokens:
        ttype = getattr(tok, "type", "")
        tmap = getattr(tok, "map", None)
        if ttype == "html_block" and tmap:
            start, end = slice_lines(tmap[0], tmap[1])
            scan(text[start:end], start, [], (tmap[0], tmap[1]))
        elif ttype == "inline" and tmap:
            content = getattr(tok, "content", "")
            if not content:
                continue
            start_line, end_line = tmap
            end_line = min(end_line, n_lines)
            seg_start, seg_end = slice_lines(start_line, end_line)
            idx = text[seg_start:seg_end].find(content)
            if idx == -1:
                continue
            abs_start = seg_start + idx
            mask = _code_mask(content, getattr(tok, "children", None) or [])
            scan(content, abs_start, mask, (start_line, end_line))

    events.sort(key=lambda e: e.start)
    return events


def _pair_wrapper_events(events: list[_TagEvent]) -> list[tuple[_TagEvent, _TagEvent]]:
    """LIFO-pair same-name open/close events in document order. A close that
    doesn't match the stack top (mismatched nesting, or nothing open at all)
    is left unpaired — along with anything still open at the end of the
    document — and falls through to the existing per-region ``_parse_fragment``
    check, which already reports a genuinely unbalanced tag as unresolved."""
    stack: list[_TagEvent] = []
    pairs: list[tuple[_TagEvent, _TagEvent]] = []
    for ev in events:
        if not ev.is_close:
            stack.append(ev)
        elif stack and stack[-1].name == ev.name:
            pairs.append((stack.pop(), ev))
    return pairs


def _strip_cross_region_wrappers(
    text: str, line_map: list[int | None]
) -> tuple[str, list[Change], list[tuple[int, int]], set[int], list[int | None]]:
    """Strip a paired ``<div>``/``<span>`` whose opener and closer sit in
    DIFFERENT regions (different html_block tokens, or an html_block and a
    later inline block) — everything between them, including whatever block
    structure (list items, a heading, a paragraph) it happens to span, is
    left completely untouched; only the tag text itself is removed. A
    same-region pair is left alone here — the existing per-region conversion
    (``_convert_block_fragment``/``_convert_inline_content``) already handles
    those, recursively, including anything nested inside them.

    The one deliberate block-type change this produces (a notice-block
    closer glued to what was always meant to be a heading, e.g.
    ``</div></div>### Executive Summary``) is recorded under
    ``R_ARTIFACT_HEADING`` instead of ``R_ARTIFACT``/``R_HTML_INLINE`` so it
    reads as intentional in the review, not a side effect — see the module's
    proof tests for the exact shape.

    Also returns ``deliberate_ranges``: the (start, end) char spans, IN THE
    RETURNED TEXT, of every opener/closer line this pass touched — content
    that used to be raw HTML and is now exposed as real markdown structure,
    which the module's final block-structure guard (``_verify_block_
    structure``) must never compare against the ORIGINAL (there was no
    corresponding original structure to preserve there at all)."""
    try:
        tokens = _MD.parse(text)
    except Exception:  # pragma: no cover - see _convert_structural
        return text, [], [], set(), line_map

    pairs = _pair_wrapper_events(_find_div_span_events(text, tokens))
    cross = [(o, c) for (o, c) in pairs if o.region != c.region]
    if not cross:
        return text, [], [], set(), line_map

    offsets = _line_offsets(text)
    lines = text.split("\n")

    edits: list[tuple[int, int, str]] = []
    for o, c in cross:
        edits.append((o.start, o.end, ""))
        edits.append((c.start, c.end, ""))

    edits_by_line: dict[int, list[tuple[int, int, str]]] = {}
    for a, b, new in edits:
        edits_by_line.setdefault(_line_index_at(offsets, a), []).append((a, b, new))

    def line_after_edits(li: int) -> str:
        a0 = offsets[li]
        b0 = offsets[li + 1] - 1 if li + 1 < len(offsets) else len(text)
        out = []
        cursor = a0
        for a, b, new in sorted(edits_by_line.get(li, [])):
            out.append(text[cursor:a])
            out.append(new)
            cursor = b
        out.append(text[cursor:b0])
        return "".join(out)

    changes: list[Change] = []
    for o, c in cross:
        o_line = _line_index_at(offsets, o.start)
        c_line = _line_index_at(offsets, c.start)
        artifact_re = _ARTIFACT_DIV_RE if o.name == "div" else _ARTIFACT_SPAN_RE
        is_artifact = artifact_re.search(text[o.start : o.end]) is not None
        rule = R_ARTIFACT if is_artifact else R_HTML_INLINE
        new_c_line = line_after_edits(c_line)
        heading_restored = (
            is_artifact
            and _line_block_kind(lines[c_line]) != "atx-heading"
            and _line_block_kind(new_c_line) == "atx-heading"
        )
        changes.append(
            Change(rule, o_line + 1, _excerpt(lines[o_line]), _excerpt(line_after_edits(o_line)))
        )
        changes.append(
            Change(
                R_ARTIFACT_HEADING if heading_restored else rule,
                c_line + 1,
                _excerpt(lines[c_line]),
                _excerpt(new_c_line),
            )
        )

    line_map = _remap_lines(line_map, edits)
    new_text, _ = _apply_edits(text, edits)
    new_offsets = _line_offsets(new_text)

    def new_line_span(li: int) -> tuple[int, int]:
        a = new_offsets[li]
        b = new_offsets[li + 1] - 1 if li + 1 < len(new_offsets) else len(new_text)
        return a, b

    # The WHOLE region each opener/closer sits in — not just its own tag's
    # line — is exposed, former-raw-HTML content with no corresponding
    # original structure to preserve; excluding only the tag's own line
    # would leave every OTHER line of a multi-line html_block (e.g. each
    # "- item" line of an exposed list) wrongly compared against the
    # original by the final structural guard, which sees the whole
    # html_block as ONE excluded unit on the original side.
    touched_lines: set[int] = set()
    for o, c in cross:
        touched_lines.update(range(o.region_lines[0], o.region_lines[1]))
        touched_lines.update(range(c.region_lines[0], c.region_lines[1]))
    deliberate_ranges = [new_line_span(li) for li in sorted(touched_lines)]
    return new_text, changes, deliberate_ranges, touched_lines, line_map


def _convert_structural(
    text: str,
    line_map: list[int | None],
    *,
    own_hosts: frozenset[str],
    image_map: Mapping[str, str],
    bs_host: str | None = None,
    in_chapter: bool = False,
) -> tuple[
    str,
    list[Change],
    list[Issue],
    list[ImageRef],
    list[tuple[int, int]],
    set[int],
    list[int | None],
]:
    (
        text,
        changes,
        deliberate_ranges,
        orig_deliberate_lines,
        line_map,
    ) = _strip_cross_region_wrappers(text, line_map)
    unresolved: list[Issue] = []
    images: list[ImageRef] = []
    # One whole-document parse's env (resolved reference definitions),
    # reused for every region below so a reference-style/shortcut link
    # resolves correctly even though each region is processed as its own
    # small snippet — see _find_link_spans.
    env: dict[str, object] = {}
    try:
        tokens = _MD.parse(text, env)
    except Exception as e:  # pragma: no cover - markdown-it-py is not known to
        # raise on arbitrary str input, but clean_page must never raise either
        # way, so this is a documented, tested safety net (see the hypothesis
        # test asserting clean_page never raises).
        unresolved.append(Issue(line=1, explanation=f"could not parse markdown: {e}"))
        return (
            text,
            changes,
            unresolved,
            images,
            deliberate_ranges,
            orig_deliberate_lines,
            line_map,
        )

    offsets = _line_offsets(text)
    n_lines = len(text.split("\n"))

    def slice_lines(start: int, end: int) -> tuple[int, int]:
        a = offsets[start]
        b = offsets[end] if end < len(offsets) else len(text)
        return a, b

    # Parallel to `replacements`: whether that specific edit's OUTPUT is
    # deliberately-converted content (an html_block turned into a table/
    # paragraph/etc.) — carried forward as `deliberate_ranges` for the final
    # structural guard, same reasoning as in `_strip_cross_region_wrappers`.
    replacements: list[tuple[int, int, str]] = []
    is_deliberate: list[bool] = []

    for tok in tokens:
        ttype = getattr(tok, "type", "")
        tmap = getattr(tok, "map", None)
        if ttype == "html_block" and tmap:
            start, end = slice_lines(tmap[0], tmap[1])
            raw = text[start:end]
            ctx = _RenderCtx(
                own_hosts=own_hosts,
                image_map=image_map,
                bs_host=bs_host,
                in_chapter=in_chapter,
                env=env,
            )
            converted, reason = _convert_block_fragment(raw, ctx)
            if converted is None:
                unresolved.append(
                    Issue(tmap[0] + 1, _unresolved_reason(raw, f"{reason} — left as-is"))
                )
                continue
            # preserve raw's own trailing newline count so line-count stays sane
            new_text = converted + ("\n" if raw.endswith("\n") else "")
            images.extend(ctx.images)  # surfaced even when nothing textually changed
            for note in dict.fromkeys(ctx.unresolved_notes):
                unresolved.append(Issue(tmap[0] + 1, f"{note} — left as-is"))
            if new_text != raw:
                replacements.append((start, end, new_text))
                is_deliberate.append(True)
                # `text` here has the same line numbering as the module-level
                # `original` (cross-region stripping preserves line count),
                # so these line indices are directly usable by the final
                # structural guard without any further shifting.
                orig_deliberate_lines.update(range(tmap[0], tmap[1]))
                for rule in dict.fromkeys(ctx.notes):
                    changes.append(Change(rule, tmap[0] + 1, _excerpt(raw), _excerpt(converted)))

    for tok in tokens:
        if getattr(tok, "type", "") != "inline":
            continue
        tmap = getattr(tok, "map", None)
        content = getattr(tok, "content", "")
        if not tmap or not content:
            continue
        start_line, end_line = tmap
        end_line = min(end_line, n_lines)
        seg_start, seg_end = slice_lines(start_line, end_line)
        haystack = text[seg_start:seg_end]
        idx = haystack.find(content)
        if idx == -1:
            if _looks_actionable(content, env):
                unresolved.append(
                    Issue(
                        start_line + 1,
                        "could not locate this line's content precisely enough to edit it "
                        "safely (likely inside a multi-block list item) — left as-is",
                    )
                )
            continue
        abs_start = seg_start + idx
        abs_end = abs_start + len(content)
        code_mask = _code_mask(content, getattr(tok, "children", None) or [])
        ctx = _RenderCtx(
            own_hosts=own_hosts,
            image_map=image_map,
            bs_host=bs_host,
            in_chapter=in_chapter,
            env=env,
        )
        converted, reason = _convert_inline_content(content, code_mask, ctx)
        if converted is None:
            if _looks_actionable(content, env):
                unresolved.append(
                    Issue(start_line + 1, _unresolved_reason(content, f"{reason} — left as-is"))
                )
            continue
        images.extend(ctx.images)  # surfaced even when nothing textually changed
        for note in dict.fromkeys(ctx.unresolved_notes):
            unresolved.append(Issue(start_line + 1, f"{note} — left as-is"))
        if converted != content:
            converted = _guard_block_type_drift(content, converted)
            replacements.append((abs_start, abs_end, converted))
            is_deliberate.append(False)
            for rule in dict.fromkeys(ctx.notes):
                changes.append(Change(rule, start_line + 1, _excerpt(content), _excerpt(converted)))

    deliberate_ranges = _shift_ranges(deliberate_ranges, replacements)
    line_map = _remap_lines(line_map, replacements)
    ordered = sorted(zip(replacements, is_deliberate, strict=True), key=lambda p: p[0][0])
    new_text, spans = _apply_edits(text, replacements)
    for (_edit, deliberate), span in zip(ordered, spans, strict=True):
        if deliberate:
            deliberate_ranges.append(span)
    return new_text, changes, unresolved, images, deliberate_ranges, orig_deliberate_lines, line_map


def _unresolved_reason(content: str, base: str) -> str:
    """Prefer a specific explanation when a known corruption-artifact pattern
    (see module docstring) is present but malformed (no matching close tag —
    ``_parse_fragment`` would otherwise have unwrapped a well-formed one)."""
    if _ARTIFACT_SPAN_RE.search(content):
        return "leaked citation span looks malformed (no matching </span>) — " + base
    if _ARTIFACT_DIV_RE.search(content):
        return "leaked notice-block div looks malformed (no matching </div>) — " + base
    return base


def _looks_actionable(content: str, env: dict[str, object]) -> bool:
    """True if ``content`` contains something this module would otherwise
    want to act on (a known HTML tag, a link needing repair, or an unresolved
    link scheme) — used only to decide whether an unlocatable inline block is
    worth an Issue at all."""
    for m in _TAG_SCAN_RE.finditer(content):
        if m.group("name").lower() in _KNOWN_HTML_TAGS:
            return True
    for span in _find_link_spans(content, env):
        if span.syntax == "autolink" and urlsplit(span.href).scheme.lower() == "file":
            return True
        if span.is_image:
            continue
        href = span.href
        if href.strip() in ("", "/") or urlsplit(href).scheme.lower() == "file":
            return True
        if urlsplit(href).scheme.lower() not in _ALLOWED_LINK_SCHEMES:
            return True
    return bool(_ARTIFACT_SPAN_RE.search(content) or _ARTIFACT_DIV_RE.search(content))


def _code_mask(content: str, children: list[object]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for child in children:
        if getattr(child, "type", "") == "code_inline":
            c_content = getattr(child, "content", "")
            idx = content.find(c_content, cursor)
            if idx != -1:
                spans.append((idx, idx + len(c_content)))
                cursor = idx + len(c_content)
    return spans


def _convert_inline_content(
    content: str, code_mask: list[tuple[int, int]], ctx: _RenderCtx
) -> tuple[str | None, str | None]:
    """Convert one inline block's raw text: known HTML tags outside code spans
    get converted/unwrapped, native markdown links get the D5 link-scheme
    treatment, everything else (including code spans and placeholder-shaped
    tags) is left byte-identical. Returns ``(None, reason)`` if something
    unconvertible was found (caller leaves the whole block untouched)."""
    if not code_mask:
        root, ok = _parse_fragment(content)
        if not ok:
            return None, "malformed or mismatched HTML markup"
        for child in root.children:
            reason = _has_unsupported(child, ctx.env)
            if reason:
                return None, reason
        return "".join(_render_node(c, ctx) for c in root.children), None

    # Code spans present: process only the text between them; code text and
    # its own delimiters pass through completely untouched.
    out: list[str] = []
    pos = 0
    mask = sorted(code_mask)
    for a, b in mask:
        segment = content[pos:a]
        root, ok = _parse_fragment(segment)
        if not ok:
            return None, "malformed or mismatched HTML markup"
        for child in root.children:
            reason = _has_unsupported(child, ctx.env)
            if reason:
                return None, reason
        out.append("".join(_render_node(c, ctx) for c in root.children))
        out.append(content[a:b])
        pos = b
    tail = content[pos:]
    root, ok = _parse_fragment(tail)
    if not ok:
        return None, "malformed or mismatched HTML markup"
    for child in root.children:
        reason = _has_unsupported(child, ctx.env)
        if reason:
            return None, reason
    out.append("".join(_render_node(c, ctx) for c in root.children))
    return "".join(out), None


def _protected_line_ranges(text: str) -> list[tuple[int, int]]:
    """Line ranges (``[start, end)``, 0-indexed) that are code — fenced or
    indented code blocks — and must never have hygiene rules (trailing
    whitespace, NBSP, zero-width chars) applied inside them."""
    ranges: list[tuple[int, int]] = []
    try:
        tokens = _MD.parse(text)
    except Exception:  # pragma: no cover - see _convert_structural
        return ranges
    for tok in tokens:
        if getattr(tok, "type", "") in ("fence", "code_block"):
            tmap = getattr(tok, "map", None)
            if tmap:
                ranges.append((tmap[0], tmap[1]))
    return ranges


def _first_h1_line_range(text: str) -> tuple[int, int, str] | None:
    """``(start_line, end_line, heading_text)`` for a leading ``# `` H1 — only
    when it is the very FIRST top-level block in the document — else None."""
    try:
        tokens = _MD.parse(text)
    except Exception:  # pragma: no cover - see _convert_structural
        return None
    for i, tok in enumerate(tokens):
        if getattr(tok, "level", None) != 0:
            continue
        ttype = getattr(tok, "type", "")
        if not (ttype.endswith("_open") or ttype in ("hr", "fence", "code_block", "html_block")):
            continue
        if ttype == "heading_open" and getattr(tok, "tag", "") == "h1":
            tmap = getattr(tok, "map", None)
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            if tmap and inline is not None and getattr(inline, "type", "") == "inline":
                return tmap[0], tmap[1], getattr(inline, "content", "")
        return None  # first top-level block exists but isn't a matching H1
    return None


def _hygiene_pass(
    text: str,
    line_map: list[int | None],
    *,
    title: str | None,
    deliberate_ranges: list[tuple[int, int]],
) -> tuple[str, list[Change], list[tuple[int, int]], list[int | None]]:
    """Every step here is expressed as an explicit (start, end, new_text)
    edit list applied through ``_apply_edits``, rather than blind whole-text
    string operations — so ``deliberate_ranges`` (deliberately-converted
    spans carried in from earlier stages, see ``_convert_structural``) can be
    shifted forward through ``_shift_ranges`` and stay accurate for the final
    structural guard that runs after this pass (this is also why NBSP/
    control-char/trailing-whitespace hygiene, which can itself expose a
    block marker — see ``_verify_block_structure`` — must NOT be trusted to
    preserve block structure on its own: it deliberately does no such
    checking here, leaving that to the one final, whole-document pass)."""
    changes: list[Change] = []

    h1 = _first_h1_line_range(text) if title else None
    if h1 is not None:
        start, end, heading_text = h1
        if heading_text.strip().casefold() == title.strip().casefold():  # type: ignore[union-attr]
            lines = text.split("\n")
            offsets = _line_offsets(text)
            remove_end = end + 1 if end < len(lines) and lines[end].strip() == "" else end
            a = offsets[start]
            b = offsets[remove_end] if remove_end < len(offsets) else len(text)
            excerpt = _excerpt(text[a:b])
            edit = [(a, b, "")]
            text, _ = _apply_edits(text, edit)
            deliberate_ranges = _shift_ranges(deliberate_ranges, edit)
            line_map = _remap_lines(line_map, edit)
            changes.append(Change(R_TITLE_H1, start + 1, excerpt, ""))

    protected = _protected_line_ranges(text)

    def is_protected(i: int) -> bool:
        return any(a <= i < b for a, b in protected)

    lines = text.split("\n")
    offsets = _line_offsets(text)
    line_edits: list[tuple[int, int, str]] = []
    changed_ws = False
    changed_nbsp = False
    changed_ctrl = False
    for i, line in enumerate(lines):
        if is_protected(i):
            continue
        new_line = line
        if _NBSP in new_line:
            new_line = new_line.replace(_NBSP, " ")
            changed_nbsp = True
        if _CONTROL_CHAR_RE.search(new_line):
            new_line = _CONTROL_CHAR_RE.sub("", new_line)
            changed_ctrl = True
        rstripped = new_line.rstrip(" \t")
        if rstripped != new_line:
            changed_ws = True
        new_line = rstripped
        if new_line != line:
            a = offsets[i]
            line_edits.append((a, a + len(line), new_line))
    if line_edits:
        text, _ = _apply_edits(text, line_edits)
        deliberate_ranges = _shift_ranges(deliberate_ranges, line_edits)
        line_map = _remap_lines(line_map, line_edits)
    if changed_ws:
        changes.append(Change(R_TRAILING_WS, 0, "", ""))
    if changed_nbsp:
        changes.append(Change(R_NBSP, 0, "", ""))
    if changed_ctrl:
        changes.append(Change(R_CONTROL_CHAR, 0, "", ""))

    text, tail_changes, deliberate_ranges, line_map = _finalize_blank_lines_and_newline(
        text, deliberate_ranges, line_map
    )
    changes.extend(tail_changes)

    deliberate_ranges = [(a, b) for a, b in deliberate_ranges if 0 <= a < b <= len(text)]
    return text, changes, deliberate_ranges, line_map


def _finalize_blank_lines_and_newline(
    text: str,
    deliberate_ranges: list[tuple[int, int]] | None = None,
    line_map: list[int | None] | None = None,
) -> tuple[str, list[Change], list[tuple[int, int]], list[int | None]]:
    """Collapse 3+ blank lines to 2 and ensure exactly one final newline —
    factored out of ``_hygiene_pass`` so ``_strip_file_reference_definitions``
    (which runs AFTER hygiene, since a reference definition can only be
    removed once every usage has already resolved against it) can re-apply
    the same two invariants to the blank line / missing trailing newline its
    OWN line deletion can leave behind, without duplicating the logic."""
    ranges = list(deliberate_ranges) if deliberate_ranges is not None else []
    lmap = line_map if line_map is not None else []
    changes: list[Change] = []

    blank_edits = [(m.start(), m.end(), "\n\n\n") for m in _BLANK_RUN_RE.finditer(text)]
    if blank_edits:
        new_text, _ = _apply_edits(text, blank_edits)
        if new_text != text:
            changes.append(Change(R_BLANK_LINES, 0, "", ""))
            ranges = _shift_ranges(ranges, blank_edits)
            lmap = _remap_lines(lmap, blank_edits)
            text = new_text

    # "Exactly one final newline": trim every trailing newline then add back
    # exactly one — except a page that is nothing but blank lines collapses
    # to a genuinely empty body rather than a lone newline.
    core = text.rstrip("\n")
    final = core + "\n" if core != "" else ""
    if final != text:
        edit = [(len(core), len(text), final[len(core) :])]
        ranges = _shift_ranges(ranges, edit)
        lmap = _remap_lines(lmap, edit)
        changes.append(Change(R_FINAL_NEWLINE, 0, "", ""))
    text = final

    return text, changes, ranges, lmap


def _normalize_crlf(text: str) -> tuple[str, list[Change]]:
    if "\r\n" in text or "\r" in text:
        new_text = text.replace("\r\n", "\n").replace("\r", "\n")
        return new_text, [Change(R_CRLF, 0, "", "")]
    return text, []


# --- final structural guard --------------------------------------------
#
# The block-drift bug this closes: NOTHING earlier in the pipeline can be
# trusted, on its own, to preserve block structure — a link/span/artifact
# edit can expose a marker (``_guard_block_type_drift`` catches the common,
# single-line, top-level shape of that at edit time, as a fast path), but so
# can hygiene (NBSP normalization turning an inert "9.\xa0\xa0w" list-item
# CONTENT into an ordered-list-marker-shaped "9.  w" — NBSP isn't marker
# whitespace, a space is), and either can happen at ANY nesting depth (a
# list item's or blockquote's own content, not just a bare physical line).
# This is the one FINAL, whole-document check: parse the ORIGINAL and the
# fully-processed FINAL text, compare the complete nested block-type tree
# (type + nesting level — nesting is implicit in markdown-it's own token
# stream, since a container's children always sit between its own *_open/
# *_close pair), ignoring only content that came from a DELIBERATE
# conversion (``deliberate_ranges``, threaded through every earlier stage —
# html_block -> table/paragraph/list/heading, including the cross-block
# notice-div case) and the removed title heading. Wherever they diverge,
# locate the drifted line, strip whatever ENCLOSING list/blockquote marker
# legitimately belongs there, and backslash-escape the marker the remaining
# text now starts with; re-verify; repeat. A divergence this can't resolve
# by escaping is never guessed at further — it's reported under `unresolved`
# and the surrounding text is left exactly as the rest of the pipeline
# produced it.

_ANY_INDENT_RE = re.compile(r"^\s*")
_CONTAINER_OPEN_TYPES = frozenset({"list_item_open", "blockquote_open"})


def _enclosing_container_marker_len(
    tokens: Sequence[object], drift_index: int, line_idx: int, line: str
) -> int:
    """How many leading characters of ``line`` belong to the list-item/
    blockquote marker that DIRECTLY, actually encloses
    ``tokens[drift_index]`` (per the real parse) AND itself starts on
    ``line_idx`` — 0 if there is no such enclosing container (top-level
    content) or it starts on a different line. Matches ONLY the marker shape
    (bullet/ordered/blockquote) the enclosing container's OWN type says it
    must be — never re-guesses the shape from the (possibly already-drifted)
    line text itself, which is what would misfire on top-level content that
    merely LOOKS marker-shaped (see module docstring)."""
    stack: list[tuple[str, str, tuple[int, int] | None]] = []
    list_kinds: list[str] = []
    for i, tok in enumerate(tokens):
        if i == drift_index:
            break
        ttype = getattr(tok, "type", "")
        if ttype == "bullet_list_open":
            list_kinds.append("bullet")
        elif ttype == "ordered_list_open":
            list_kinds.append("ordered")
        elif ttype in ("bullet_list_close", "ordered_list_close"):
            if list_kinds:
                list_kinds.pop()
        elif ttype == "list_item_open":
            kind = list_kinds[-1] if list_kinds else "bullet"
            stack.append(("list_item_open", kind, getattr(tok, "map", None)))
        elif ttype == "list_item_close":
            if stack and stack[-1][0] == "list_item_open":
                stack.pop()
        elif ttype == "blockquote_open":
            stack.append(("blockquote_open", "blockquote", getattr(tok, "map", None)))
        elif ttype == "blockquote_close":
            if stack and stack[-1][0] == "blockquote_open":
                stack.pop()
    # Walk the stack OUTERMOST to INNERMOST, consuming each enclosing
    # container's OWN marker in turn (e.g. a blockquote directly containing
    # a list item on the SAME physical line, "> - text", needs BOTH "> " and
    # "- " stripped, in that order). An OUTER container that does not itself
    # start on this exact line is simply a multi-line container whose own
    # opening line is earlier — SKIPPED, not a reason to stop (its marker
    # isn't on THIS line at all) — e.g. an outer top-level item spanning
    # several lines, with an inner item opening fresh partway through it.
    # Once a container that DOES start here has been consumed, a FURTHER
    # container that doesn't (shouldn't normally happen — nesting only gets
    # deeper going inward) stops the walk defensively.
    base = 0
    started = False
    for _etype, kind, tmap in stack:
        if not tmap or tmap[0] != line_idx:
            if started:
                break
            continue
        started = True
        rest = line[base:]
        indent = _ANY_INDENT_RE.match(rest)
        indent_len = indent.end() if indent else 0
        after_indent = rest[indent_len:]
        if kind == "blockquote":
            if not after_indent.startswith(">"):
                break
            end = 1
        elif kind == "ordered":
            m = _ORDERED_MARK_RE.match(after_indent)
            if not m:
                break
            end = m.end()
        else:
            m = _BULLET_MARK_RE.match(after_indent)
            if not m:
                break
            end = m.end()
        if end < len(after_indent) and after_indent[end] in " \t":
            end += 1
        base += indent_len + end
    return base


def _block_starts_by_line(
    tokens: Sequence[object],
) -> tuple[dict[int, tuple[str, ...]], dict[int, list[tuple[str, int]]]]:
    """For every line where one or more container/leaf blocks BEGIN: its
    "block signature" — the FULL ancestor chain, root first, down to the
    deepest thing starting there, e.g. ``{5: ("bullet_list", "list_item",
    "paragraph")}`` for a line reading "- text" that opens a fresh list —
    tracked with a running stack so a container inherited from an EARLIER
    line (a paragraph that ends up absorbed as a second, loose paragraph of
    a list item that opened several lines above it, say) still shows up in
    the chain even though its own opening token doesn't start here; that
    distinction matters, since a top-level paragraph silently becoming
    nested inside a preceding list this way (real shape: hygiene's own NBSP
    normalization handing a line just enough indentation to read as list-
    item continuation) is exactly the drift this comparison exists to catch,
    and a signature built ONLY from tokens whose own map starts on this line
    would miss it completely (both sides would show a lone ``("paragraph",)``
    even though one is top-level and the other buried inside a list).

    Also returns, in parallel, ``own_starts``: for every such line, the
    ordered list of ``(name, token_index)`` pairs for tokens whose OWN
    ``.map[0]`` is this exact line — i.e. only the SUFFIX of the full chain
    that actually begins here, which is what a repair can locate a real
    token (and therefore an enclosing-container marker length) for; the
    PREFIX of the chain inherited from an earlier line has no token here to
    escape at all."""
    full_sig: dict[int, tuple[str, ...]] = {}
    own_starts: dict[int, list[tuple[str, int]]] = {}
    stack: list[str] = []
    for i, tok in enumerate(tokens):
        ttype = getattr(tok, "type", "")
        if ttype == "inline":
            continue
        if ttype.endswith("_close"):
            if stack:
                stack.pop()
            continue
        tmap = getattr(tok, "map", None)
        if ttype.endswith("_open"):
            name = ttype[: -len("_open")]
            if tmap:
                full_sig[tmap[0]] = (*stack, name)
                own_starts.setdefault(tmap[0], []).append((name, i))
            stack.append(name)
            continue
        # self-contained leaf: hr, fence, code_block, html_block
        if tmap:
            full_sig[tmap[0]] = (*stack, ttype)
            own_starts.setdefault(tmap[0], []).append((ttype, i))
    return full_sig, own_starts


def _line_already_this_kind_in_original(original: str, candidate_line: str, kind: str) -> bool:
    """True if ``candidate_line`` (about to be escaped because it currently
    classifies as ``kind``) corresponds — modulo only the hygiene-level
    changes this module documents (NBSP normalized to space, trailing
    whitespace stripped — never anything content-changing) — to a line that
    ALREADY, in the untouched ``original`` text, classified as that SAME
    kind. Escaping such a line would turn a genuine, pre-existing heading/
    list/etc. into literal text, which this module must never do: "it never
    escapes a marker on a line whose block type in the ORIGINAL parse is
    that same block type." Whatever divergence led the repair loop here is a
    misalignment elsewhere, not real drift at this specific line."""
    normalized_candidate = candidate_line.replace(_NBSP, " ").rstrip(" \t")
    for line in original.split("\n"):
        if (
            line.replace(_NBSP, " ").rstrip(" \t") == normalized_candidate
            and _line_block_kind(line) == kind
        ):
            return True
    return False


def _verify_block_structure(
    original: str,
    final_text: str,
    line_map: list[int | None],
    *,
    title: str | None,
    deliberate_ranges: list[tuple[int, int]],
    orig_deliberate_lines: set[int],
) -> tuple[str, list[Change], list[Issue]]:
    """The final, whole-document structural guard (see the module docstring
    section above this function). Compares PER LINE, not as one long token
    sequence: for every ORIGINAL line, ``line_map`` (built by every earlier
    stage from its own (start, end, new_text) edits — see ``_remap_lines``)
    gives the char offset that line's content now starts at in
    ``final_text``; that line's "block signature" in the ORIGINAL parse (the
    stack of container/leaf block types that START on it — see
    ``_block_starts_by_line``) must equal its mapped line's signature in the
    FINAL parse. A sequence-alignment approach (the previous design) lets a
    SINGLE divergence anywhere desynchronize every comparison after it,
    blaming the wrong line and, on rollback, discarding unrelated repairs
    that were already correct — comparing by ``line_map`` instead means one
    line's outcome can never depend on any other line's.

    Two invariants, both required:

    (a) A line never gets escaped if the ORIGINAL parse already treated it
        (modulo only NBSP/trailing-whitespace hygiene) as that SAME block
        type — see ``_line_already_this_kind_in_original``. A genuine
        heading stays a heading no matter what else on the page diverges.
    (b) Each mismatching line is repaired OR left completely alone, ON ITS
        OWN — there is no whole-document rollback here at all: a line this
        module can't safely repair is reported under ``unresolved`` and left
        byte-identical, while every OTHER line's already-successful repair
        is kept regardless of what happens elsewhere on the page.

    Exactly two parses total (``original`` and ``final_text``, each parsed
    once) — a repair is a same-line backslash-escape, and this module's
    marker set never lets escaping one line change how any OTHER line
    parses (the one documented exception, a setext heading underline
    changing how the line above it reads, is out of scope for the marker
    shapes a legacy wiki export actually produces — see the module
    docstring), so no re-parse is needed to confirm a repair worked: a local
    ``_line_block_kind`` check on the escaped line is enough."""
    changes: list[Change] = []
    unresolved: list[Issue] = []

    try:
        orig_tokens = _MD.parse(original)
        final_tokens = _MD.parse(final_text)
    except Exception:  # pragma: no cover - see _convert_structural
        return final_text, changes, unresolved

    orig_sig, _orig_own_starts = _block_starts_by_line(orig_tokens)
    final_sig, final_own_starts = _block_starts_by_line(final_tokens)

    orig_offsets = _line_offsets(original)
    final_offsets = _line_offsets(final_text)
    n_orig_lines = len(orig_offsets)

    orig_exclude: set[int] = set(orig_deliberate_lines)
    if title:
        h1 = _first_h1_line_range(original)
        if h1 is not None and h1[2].strip().casefold() == title.strip().casefold():
            orig_exclude.update(range(h1[0], h1[1]))

    final_exclude: set[int] = set()
    for a, b in deliberate_ranges:
        la = _line_index_at(final_offsets, a)
        lb = _line_index_at(final_offsets, max(b - 1, a))
        final_exclude.update(range(la, lb + 1))

    lines = final_text.split("\n")
    accounted: set[int] = {
        _line_index_at(final_offsets, c) for c in line_map[:n_orig_lines] if c is not None
    }

    def repair_or_report(report_oi: int | None, fi: int, osig: tuple[str, ...]) -> None:
        fsig = final_sig.get(fi, ())
        if fsig == osig:
            return
        report_line = (report_oi + 1) if report_oi is not None else (fi + 1)
        k = 0
        while k < len(osig) and k < len(fsig) and osig[k] == fsig[k]:
            k += 1
        own = final_own_starts.get(fi, [])
        own_start_depth = len(fsig) - len(own)
        if k >= len(fsig) or k < own_start_depth:
            # Either nothing EXTRA on the final side to strip a marker from
            # (the final signature is a strict prefix of, or otherwise no
            # longer than, the original's), or the divergence sits in the
            # part of the chain INHERITED from an earlier line (a container
            # that opened before this one and has no token of its own here
            # to escape — e.g. a paragraph silently absorbed into a
            # preceding list's continuation, see _block_starts_by_line).
            # Neither is something this module can repair by escaping.
            unresolved.append(
                Issue(
                    report_line,
                    "block structure diverged from the original at this line and "
                    "could not be safely repaired — left as-is",
                )
            )
            return
        _name, drift_index = own[k - own_start_depth]
        line = lines[fi]
        prefix_len = _enclosing_container_marker_len(final_tokens, drift_index, fi, line)
        prefix, rest = line[:prefix_len], line[prefix_len:]
        rest_kind = _line_block_kind(rest)
        new_rest = _escape_block_marker(rest)
        if new_rest == rest or (
            rest_kind is not None and _line_already_this_kind_in_original(original, line, rest_kind)
        ):
            unresolved.append(
                Issue(
                    report_line,
                    "block structure diverged from the original here and this "
                    "module doesn't know how to repair it by escaping — left as-is",
                )
            )
            return
        new_line = prefix + new_rest
        changes.append(Change(R_BLOCK_DRIFT, fi + 1, _excerpt(line), _excerpt(new_line)))
        lines[fi] = new_line

    orig_lines = original.split("\n")
    for oi in range(n_orig_lines):
        if oi in orig_exclude:
            continue
        osig = orig_sig.get(oi, ())
        fi_char = line_map[oi] if oi < len(line_map) else None
        if fi_char is None:
            # A line whose ENTIRE original content was NBSP/whitespace (still
            # a real, non-blank paragraph to the ORIGINAL parse, since NBSP
            # isn't CommonMark blank-line whitespace) legitimately vanishes
            # once hygiene normalizes it away and the blank-line collapse
            # sweeps it up with its neighbours — an invisible paragraph
            # disappearing is exactly this module's intended hygiene, not a
            # structural drift, so it is never reported here.
            invisible = oi < len(orig_lines) and orig_lines[oi].replace(_NBSP, " ").strip() == ""
            if osig and not invisible:
                unresolved.append(
                    Issue(
                        oi + 1,
                        "this line is no longer present in the cleaned text and "
                        "could not be safely repaired — left as-is",
                    )
                )
            continue
        fi = _line_index_at(final_offsets, fi_char)
        if fi in final_exclude:
            continue
        repair_or_report(oi, fi, osig)

    # Reverse check: a block start in the final text that no original line
    # maps to at all (e.g. a `<br>` hard-break introducing a brand-new
    # physical line) and that isn't inside a deliberately-converted span is
    # just as much a drift as a mismatched one — report/repair it against
    # the nearest PRECEDING original line for a meaningful line number.
    mapped_pairs = sorted((c, oi) for oi, c in enumerate(line_map[:n_orig_lines]) if c is not None)
    mapped_chars = [c for c, _oi in mapped_pairs]
    for fi in sorted(final_sig):
        if fi in accounted or fi in final_exclude:
            continue
        pos = bisect.bisect_right(mapped_chars, final_offsets[fi]) - 1
        nearest_oi = mapped_pairs[pos][1] if pos >= 0 else None
        repair_or_report(nearest_oi, fi, ())

    if not changes:
        return final_text, changes, unresolved
    return "\n".join(lines), changes, unresolved


def _strip_file_reference_definitions(text: str) -> tuple[str, list[Change]]:
    """Remove a REFERENCE DEFINITION (``[ref]: file:///...``) whose target
    has a ``file:`` scheme — this module's equivalent, for the reference-
    style form, of stripping a direct ``[text](file:///...)`` link's target.

    Reference definitions produce no token of their own in ANY parse — they
    are pure block-level metadata markdown-it consumes into ``env`` (see
    ``_find_link_spans``) — so removing one can never introduce block-type
    drift for anything around it, and this deliberately runs LAST, after
    every USAGE of the reference has already been resolved and rewritten by
    the normal per-region pipeline: removing the definition any earlier
    would leave ``[text][ref]`` referring to nothing, unresolvable."""
    env: dict[str, object] = {}
    try:
        _MD.parse(text, env)
    except Exception:  # pragma: no cover - see _convert_structural
        return text, []
    references = env.get("references")
    if not isinstance(references, dict) or not references:
        return text, []
    offsets = _line_offsets(text)
    n_lines = len(text.split("\n"))
    edits: list[tuple[int, int, str]] = []
    changes: list[Change] = []
    for ref in references.values():
        if not isinstance(ref, dict):
            continue
        href = str(ref.get("href", ""))
        if urlsplit(href).scheme.lower() != "file":
            continue
        rmap = ref.get("map")
        if not rmap or len(rmap) != 2:
            continue
        start_line, end_line = int(rmap[0]), int(rmap[1])
        if not (0 <= start_line < end_line <= n_lines):
            continue
        a = offsets[start_line]
        b = offsets[end_line] if end_line < len(offsets) else len(text)
        excerpt = _excerpt(text[a:b])
        edits.append((a, b, ""))
        changes.append(Change(R_FILE_LINK, start_line + 1, excerpt, ""))
    if not edits:
        return text, []
    new_text, _ = _apply_edits(text, edits)
    return new_text, changes


def clean_page(
    md: str,
    *,
    title: str | None = None,
    own_hosts: Iterable[str] = (),
    bs_host: str | None = None,
    in_chapter: bool = False,
    image_map: Mapping[str, str] | None = None,
) -> CleanResult:
    """Clean one BookStack wiki page BODY (not the whole file — no
    frontmatter) against the rework's new wiki-body rules.

    Never raises: any input this module can't make sense of is left
    byte-identical in the output and reported as an :class:`Issue` instead
    (or, if it isn't even recognizable as something the rules cover, silently
    left alone — see the module docstring's placeholder-trap note). Pure and
    offline: no filesystem or network access, deterministic on the same
    input.

    ``title``, if given, causes a leading ``# <title>`` heading that merely
    repeats the page title (case/whitespace-insensitive match) to be removed.

    ``own_hosts`` is the set of ``host[:port]`` values (as
    ``urllib.parse.urlsplit(...).netloc`` would report them) that identify
    THIS wiki's own image-gallery uploads; an absolute
    ``https://<own-host>/uploads/images/...`` reference is recognized as one.
    Real BookStack pages also store a HOST-RELATIVE spelling of the same
    reference, ``/uploads/images/...`` (no scheme/host at all) — ``bs_host``
    (a single ``host[:port]``, no scheme) is the workspace's own BookStack
    host, used ONLY to turn that host-relative spelling into the SAME
    canonical absolute URL an ``own_hosts``-qualified one already resolves
    to, so both spellings of the same reference key into ``image_map``/
    ``CleanResult.images`` identically; a host-relative reference is left
    unrecognized (today's behavior) when ``bs_host`` isn't given. Either
    recognized form is reported in ``CleanResult.images`` with a proposed
    local filename. ``image_map``, if given, maps the CANONICAL absolute URL
    to the local relative path already downloaded for it (by a caller with
    network access) — when that URL is a key in this mapping, the reference
    is rewritten in place to ``![caption](images/<file>)`` (book root) or
    ``![caption](../images/<file>)`` (``in_chapter=True`` — REF-007: a page
    that sits inside a chapter needs the one-level-up spelling) instead of
    merely being reported.
    """
    changes: list[Change] = []
    unresolved: list[Issue] = []
    images: list[ImageRef] = []

    text, ch = _normalize_crlf(md)
    changes.extend(ch)
    original = text  # the final structural guard's comparison baseline
    # Original-line-index -> current char offset (or None), threaded through
    # every stage below via _remap_lines and consumed only by the final
    # structural guard — see _verify_block_structure.
    line_map: list[int | None] = list(_line_offsets(original))

    hosts = frozenset(own_hosts)
    mapping = image_map or {}
    text, ch, un, img, deliberate, orig_deliberate_lines, line_map = _convert_structural(
        text,
        line_map,
        own_hosts=hosts,
        image_map=mapping,
        bs_host=bs_host,
        in_chapter=in_chapter,
    )
    changes.extend(ch)
    unresolved.extend(un)
    images.extend(img)

    text, ch, deliberate, line_map = _hygiene_pass(
        text, line_map, title=title, deliberate_ranges=deliberate
    )
    changes.extend(ch)

    text, ch, un = _verify_block_structure(
        original,
        text,
        line_map,
        title=title,
        deliberate_ranges=deliberate,
        orig_deliberate_lines=orig_deliberate_lines,
    )
    changes.extend(ch)
    unresolved.extend(un)

    text, ch = _strip_file_reference_definitions(text)
    changes.extend(ch)
    if ch:
        text, ch, _ranges, _line_map = _finalize_blank_lines_and_newline(text)
        changes.extend(ch)

    return CleanResult(text=text, changes=changes, unresolved=unresolved, images=images)


def visible_text(md: str) -> str:
    """A whitespace-normalized, tag-free projection of ``md``'s reader-visible
    text — used by tests to confirm a cleanup transform preserved what a
    reader actually sees. Handles both plain markdown and markdown containing
    raw HTML (which core markdown-it-py, without the GFM table plugin this
    repo doesn't depend on, would otherwise render pipe-table syntax as
    literal text rather than parse it — so this recognizes GFM pipe-table
    shape itself, well enough for equivalence checks, rather than depending on
    real table parsing)."""
    env: dict[str, object] = {}
    try:
        tokens = _MD.parse(md, env)
    except Exception:  # pragma: no cover
        return _normalize_ws(_strip_tags(md))
    parts: list[str] = []
    for tok in tokens:
        ttype = getattr(tok, "type", "")
        if ttype in ("fence", "code_block"):
            parts.append(getattr(tok, "content", ""))
        elif ttype == "html_block":
            parts.append(_strip_tags(getattr(tok, "content", "")))
        elif ttype == "inline":
            content = getattr(tok, "content", "")
            parts.append(_visible_text_of_inline(content, env))
    return _normalize_ws(" ".join(parts))


_PIPE_ROW_RE = re.compile(r"^\s*\|?(.+?)\|?\s*$")
_PIPE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _visible_text_of_inline(content: str, env: dict[str, object]) -> str:
    lines = content.split("\n")
    if len(lines) >= 2 and any(_PIPE_SEP_RE.match(line) for line in lines):
        cells: list[str] = []
        for line in lines:
            if _PIPE_SEP_RE.match(line):
                continue
            m = _PIPE_ROW_RE.match(line)
            row = m.group(1) if m else line
            cells.extend(c.strip() for c in row.split("|"))
        return " ".join(_strip_md_inline(_strip_tags(c), env) for c in cells if c.strip())
    return _strip_md_inline(_strip_tags(content), env)


_TAG_ONLY_RE = re.compile(r"<[^<>]*>")
_BLOCK_SEPARATOR_TAGS = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "p", "div", "br", "li", "ul", "ol"}
)
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")
_IMG_ALT_RE = re.compile(r"""alt\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _strip_tags(text: str) -> str:
    """Remove HTML tags, inserting a separating space only for block-level
    tags (a real renderer visually separates ``<td>a</td><td>b</td>`` even
    with no whitespace in the source) — a purely inline wrapper like
    ``<span>``/``<strong>`` contributes no space of its own.

    An ``<img alt="...">``'s alt text is extracted as its own visible text
    (matching how this module's own markdown-image handling treats
    ``![alt](url)`` as visible text) — otherwise a raw ``<img>`` tag getting
    converted to a markdown image line would look like it *gained* text this
    module never saw before, when really both sides always carried it, just
    one as an attribute and one as markdown alt text."""

    def repl(m: re.Match[str]) -> str:
        tag_text = m.group(0)
        name_m = _TAG_NAME_RE.match(tag_text)
        name = name_m.group(1).lower() if name_m else ""
        if name == "img":
            alt_m = _IMG_ALT_RE.search(tag_text)
            if alt_m:
                alt = alt_m.group(2) if alt_m.group(2) is not None else alt_m.group(3)
                return f" {alt} "
        return " " if name in _BLOCK_SEPARATOR_TAGS else ""

    return html.unescape(_TAG_ONLY_RE.sub(repl, text))


_MD_ESCAPE_RE = re.compile(r"""\\([!"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])""")
_MD_INLINE_MARK_RE = re.compile(r"\*\*\*|\*\*|\*|__|_|`|\\")


def _strip_md_inline(text: str, env: dict[str, object]) -> str:
    """Strip markdown inline syntax down to plain text — a backslash-escaped
    ASCII punctuation character (CommonMark's only escape form, e.g. ``\\.``
    in an ordered-list-marker escape this module itself introduces) resolves
    to that literal character FIRST, in its own pass, so it survives rather
    than being eaten by the generic emphasis-marker stripping below. Links/
    images are replaced by their own text/alt (an autolink's "text" is its
    own URL, matching what a reader actually sees for ``<scheme:...>``
    syntax) via the same token-based locator the rest of this module uses —
    never a regex, for the same bracket-nesting/reference-style reasons."""
    spans = _find_link_spans(text, env)
    if spans:
        out = []
        cursor = 0
        for span in spans:
            out.append(text[cursor : span.start])
            out.append(span.text)
            cursor = span.end
        out.append(text[cursor:])
        text = "".join(out)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    text = _MD_INLINE_MARK_RE.sub("", text)
    return text


def _normalize_ws(text: str) -> str:
    # Zero-width/bidi-control characters (see _CONTROL_CHAR_RE) are invisible
    # by definition — a reader comparing "what does the page look like"
    # before and after this module strips them (R_CONTROL_CHAR) sees no
    # difference, so visible_text() shouldn't either.
    text = _CONTROL_CHAR_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# --- human-readable review report ----------------------------------------


def render_review(
    changes: Iterable[Change],
    unresolved: Iterable[Issue],
    images: Iterable[ImageRef] = (),
) -> str:
    """Render a human-readable cleanup review: what changed (grouped by rule,
    with line numbers), what's left for a human to fix by hand, and — pass
    through ``CleanResult.images`` here — which absolute image-gallery URLs
    (the ones that matched an ``own_hosts`` entry passed to ``clean_page``)
    will be downloaded into ``images/`` and rewritten once a caller with
    network access supplies the ``url -> local path`` mapping. Meant for the
    owner to read before anything produced by this module is pushed
    (D5: "reviewed as a diff")."""
    changes = list(changes)
    unresolved = list(unresolved)
    images = list(images)
    lines: list[str] = []
    if changes:
        lines.append(f"Changes ({len(changes)}):")
        by_rule: dict[str, list[Change]] = {}
        for c in changes:
            by_rule.setdefault(c.rule, []).append(c)
        for rule in sorted(by_rule):
            group = by_rule[rule]
            lines.append(f"  {rule} ({len(group)}):")
            for c in group:
                if c.before or c.after:
                    lines.append(f"    line {c.line}: {c.before!r} -> {c.after!r}")
                else:
                    lines.append(f"    line {c.line}")
    else:
        lines.append("Changes: none")
    if unresolved:
        lines.append(f"Left for a human ({len(unresolved)}):")
        for i in sorted(unresolved, key=lambda x: x.line):
            lines.append(f"  line {i.line}: {i.explanation}")
    else:
        lines.append("Left for a human: none")
    if images:
        lines.append(f"Images to fetch into images/ ({len(images)}):")
        for img in sorted(images, key=lambda x: x.url):
            lines.append(f"  {img.url} -> images/{img.proposed_filename}")
    else:
        lines.append("Images to fetch into images/: none")
    return "\n".join(lines) + "\n"
