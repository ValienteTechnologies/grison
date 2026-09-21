"""Direction-neutral text-level markdown/HTML escaping helpers: HTML-escaping
plain text (:func:`_esc`), backslash-escaping literal text so it survives a
markdown-it reparse unchanged (:func:`_md_escape_run` and friends), wrapping
content in a ``*``/``**`` emphasis delimiter (:func:`_wrap_delim`), finalizing
a rendered markdown line (:func:`_finalize_line`), and fencing inline code
content (:func:`_fence_code`).
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


def _finalize_line(text: str, on_loss: Callable[[str], None] | None = None) -> str:
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
    fence either.

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
        lines.append(_md_escape_line_start(stripped))
    return "\n".join(lines)


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
