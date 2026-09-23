"""Direction-neutral text-level markdown/HTML escaping helpers: HTML-escaping
plain text (:func:`_esc`), backslash-escaping literal text so it survives a
markdown-it reparse unchanged (:func:`_md_escape_run` and friends), wrapping
content in a ``*``/``**`` emphasis delimiter (:func:`_wrap_delim`), finalizing
a rendered markdown line (:func:`_finalize_line`), and choosing a backtick
fence long enough to safely wrap literal text that itself contains backticks
(:func:`_backtick_fence`, used by both :func:`_fence_code` here and
``from_html/fence.py``'s ``_fence_marker_for``).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from grison.markdown.converter.mdparse import _parse_inline_tree
from grison.markdown.converter.nodes import _report_loss


def _esc(text: str) -> str:
    """HTML-escape text/attribute content. ``"`` is escaped too so a URL containing a
    quote can't break out of the ``href="…"`` attribute and inject markup."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
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
    return (
        len(tree.children) == 1
        and tree.children[0].type == "text"
        and (tree.children[0].content == text)
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
    assert m is not None, "content.strip() != '' guarantees the whitespace-run regex matches"
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    return f"{lead}{marker}{core}{marker}{trail}"


def escape_literal_text_to_md(text: str) -> str:
    """Turn a blob of literal, already-HTML-unescaped plain text (one or more
    physical lines, ``\\n``-joined) into markdown that round-trips back to
    this text: each line goes through :func:`_md_escape_run` (protects a run
    that would otherwise be reparsed as emphasis/code/a link/raw inline HTML —
    e.g. a literal ``<script>...</script>`` string sitting in running text)
    and then :func:`_finalize_line` — the SAME finishing step the converter's
    own ``html->markdown`` direction applies to every rendered line (see
    below) — which strips leading AND trailing whitespace per line (4+
    leading spaces would otherwise read back as an indented-code-block
    attempt on the next push; see :func:`_finalize_line`'s own docstring for
    the full reasoning, including why the strip is Unicode-aware) and then
    escapes a line-leading run that would otherwise be reparsed as a block
    sigil — heading/list marker/blockquote/fence/GFM table row/thematic
    break. The round-trip guarantee is against that STRIPPED text, not
    necessarily the exact original whitespace — matching what the very next
    ``md_to_html``/``html_to_md`` pass would do to whitespace at a line edge
    regardless.

    The converter's own ``html->markdown`` direction gets this guarantee for
    free (every text node it emits goes through the same two primitives — see
    ``from_html/inline.py``'s ``_render_text_run`` and this module's own
    :func:`_finalize_line`, called from ``from_html/blocks.py``). This
    function exists for callers OUTSIDE the HTML<->markdown converter that
    still need to hand it plain text destined for a markdown field:
    ``grison.markdown.mapping._prose_to_md``'s ``ConverterError`` fallback
    degrades HTML falling outside the converter's closed vocabulary (e.g. a
    scanner-sourced ``<b>``/``<i>`` tag) down to flat text — that text still
    lands in a real markdown field and needs the exact same round-trip-safety
    guarantee as everything the converter itself emits, not a hand-rolled
    substitute (a bare ``<script>...</script>`` left unescaped there is real
    inline HTML once re-parsed, rejected by the validator's FND-014 rule)."""
    return "\n".join(_finalize_line(_md_escape_run(line)) for line in text.split("\n"))


def _md_escape_quotes(text: str) -> str:
    """Backslash-escape a literal ``"`` in a link title so it can't prematurely
    close the title's own quoted markdown syntax."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


_LINE_START_SIGIL_RE = re.compile(r"^(#{1,6}(?=[ \t]|$)|[-*+](?=[ \t]|$)|\d+\.(?=[ \t]|$)|>|```)")
# CommonMark's thematic-break shape: 0-3 leading spaces (never actually present
# here — callers always strip first), then 3+ of the SAME ``-``/``*``/``_``
# character with only spaces/tabs allowed between occurrences, and nothing else
# on the line. A run of just 1-2 is ordinary text (or, for ``-``, already
# caught by ``_LINE_START_SIGIL_RE`` as a list marker when followed by a
# space); this only needs to catch the 3-or-more case that rule doesn't.
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")


def _md_escape_line_start(line: str, *, escape_pipe_and_thematic_break: bool = True) -> str:
    """Escape a line-leading sequence that would otherwise be read as a block
    sigil (heading/list marker/blockquote/fence/GFM table row/thematic break)
    once this text starts a fresh output line. Only the first matched
    character needs the backslash in every case — CommonMark treats an
    escaped punctuation character as ordinary text, which is enough to stop
    the whole-line pattern from matching.

    ``escape_pipe_and_thematic_break`` gates the last two cases only — a
    leading ``|`` (GFM table row shape) and a thematic-break-shaped line
    (``---``/``***``/``___``, optionally space-separated, 3+ characters,
    CommonMark's own definition, see :data:`_THEMATIC_BREAK_RE`). A caller
    that's about to embed this text INSIDE a GFM table cell — never written
    out as its own top-level line — passes ``False``: a table cell already
    backslash-escapes every ``|`` in its content on its own (see
    ``from_html/table.py``'s ``_render_cell``; escaping a leading one again
    here would double the backslash instead of protecting it), and cell
    content can never be misread as a thematic break in the first place,
    since it never starts a markdown line of its own."""
    if escape_pipe_and_thematic_break and (
        line.startswith("|") or _THEMATIC_BREAK_RE.match(line) is not None
    ):
        return "\\" + line
    m = _LINE_START_SIGIL_RE.match(line)
    if not m:
        return line
    pos = m.end() - 1 if m.group(0) not in ("```",) else 0
    return line[:pos] + "\\" + line[pos:]


def _finalize_line(
    text: str,
    on_loss: Callable[[str], None] | None = None,
    *,
    escape_pipe_and_thematic_break: bool = True,
) -> str:
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
    fence/GFM table row/thematic break either (the last two are gated by
    ``escape_pipe_and_thematic_break`` — see :func:`_md_escape_line_start`;
    ``from_html/table.py``'s ``_render_cell`` passes ``False`` since a table
    cell's own pipe-escaping and line-embedding already make both moot).

    The strip uses Python's full (Unicode-aware) whitespace definition, matching
    markdown-it-py's own ``rules_block/paragraph.py`` (``state.getLines(...).strip()``
    — plain ``str.strip()``, same character class) — NOT just the ASCII space:
    a non-breaking space (``\\xa0``, real corpus data — TipTap/GW's own editor
    leaves stray ``&nbsp;`` runs in stored HTML) left in literally would round-trip
    as real markdown content, but the very next ``md_to_html`` re-parse silently
    trims it away as commonmark-insignificant regardless — a construct that never
    reaches a stable fixpoint otherwise. Reported via ``on_loss`` only when
    something OTHER than a plain ASCII space gets dropped this way (a plain-space
    trim is not, matching this function's own prior behavior — see module
    docstring's normalization list)."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped != line.strip(" "):
            _report_loss(
                on_loss,
                "non-breaking or other non-ASCII whitespace at a line edge dropped "
                "(normalization; matches CommonMark's own paragraph-content trim)",
            )
        lines.append(
            _md_escape_line_start(
                stripped, escape_pipe_and_thematic_break=escape_pipe_and_thematic_break
            )
        )
    return "\n".join(lines)


def _backtick_fence(text: str, min_length: int) -> str:
    """A backtick fence for wrapping/framing literal ``text`` that may itself
    contain backticks: one longer than the longest backtick run already
    present (CommonMark's own convention — see :func:`_fence_code` and
    ``from_html/fence.py``'s ``_fence_marker_for``), so no run inside ``text``
    can ever be mistaken for, or collide with, the fence itself — but never
    shorter than ``min_length``, since an inline code SPAN's own CommonMark
    minimum is a single backtick while a FENCED code BLOCK's is three."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(min_length, longest + 1)


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
    fence = _backtick_fence(text, 1)
    if text.startswith("`"):
        text = " " + text
    if text.endswith("`"):
        text = text + " "
    if text.startswith(" ") and text.endswith(" ") and text.strip(" ") != "":
        text = " " + text + " "
    return f"{fence}{text}{fence}"
