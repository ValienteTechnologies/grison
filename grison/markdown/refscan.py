"""One shared reference/embed scanner over a real markdown-it token stream — D1
(Ghostwriter evidence) and D9 (BookStack wiki images) both flow through this, and it's
what :mod:`grison.validator` uses for the cross-document checks (stem collisions,
caption conflicts) a per-field/per-page converter call can't see on its own. Built on
the identical ``MarkdownIt("commonmark")`` configuration
:mod:`grison.markdown.converter` uses, so a scanner result and the converter's own
verdict about the same document never disagree about what counts as "inside a code
fence/span" (code content is never inline-parsed by markdown-it in the first place —
an image or link written inside one is invisible to this scanner by construction, not
by a regex heuristic).

This module does no resolution and no validation of its own — it only reports what
markdown *syntax* is present and where. :mod:`grison.validator` (and, later, the sync
engine) decide what a given path/caption/position combination means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# A dedicated instance (not grison.markdown.converter's) — never reconfigured, so
# nothing here is coupled to anything the converter module might change later.
_MD = MarkdownIt("commonmark")

RefKind = Literal["embed", "cross_reference"]


@dataclass(frozen=True)
class FoundRef:
    """One image (``kind="embed"``) or plain link (``kind="cross_reference"``) found
    in a document's markdown.

    ``path`` is the destination exactly as written (``src``/``href``, unresolved).
    ``caption`` is the image's alt text / the link's visible text, plain (markup
    stripped, e.g. ``**bold**`` -> ``bold``). ``title`` is the image's/link's
    ``"..."`` title, or ``""`` when absent. ``line`` is 1-based. ``standalone`` is
    True only for an embed whose containing paragraph/list-item block has NO other
    content at all (D1's "valid position" requirement) — always False for a
    cross-reference, which is inline by nature. ``in_list_item`` is True when the
    reference sits anywhere inside a list item (at any nesting depth)."""

    kind: RefKind
    path: str
    caption: str
    title: str
    line: int
    standalone: bool
    in_list_item: bool


def decode_ref_path(raw: str) -> str:
    """markdown-it-py's link normalisation percent-encodes non-ASCII bytes in an
    image/link destination (``evidence/Sonu%C3%A7lar%C4%B1.png`` for the author's
    own ``evidence/Sonuçları.png``) — every consumer that resolves a destination
    against the filesystem, an index, or another path's spelling needs the decoded
    (identity) form back, not the encoded one markdown-it hands out. Used here (so
    every :class:`FoundRef` already carries the decoded form) and independently by
    a ``RefResolver`` that a destination can reach through a DIFFERENT markdown-it
    parse — :mod:`grison.markdown.converter`'s own, not this module's — such as
    :class:`grison.validator.refs.OfflineEvidenceResolver` or
    :class:`grison.adapters._gw_common.IndexRefResolver`.

    Left undecoded (still percent-encoded) when the percent-encoding names bytes
    that are not valid UTF-8 — never raised out of here for every caller to catch:
    a destination in that shape can never name a real file/index entry either way,
    so it surfaces through each caller's EXISTING "does not resolve" validation
    failure, with the bogus escaped text visible in the message — a validation
    failure with a clear message, without inventing a second failure path callers
    outside this fix's lane would also need to wire up."""
    try:
        return unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return raw


def code_spans(md: str) -> list[str]:
    """Every inline code span's/fenced-or-indented code block's own raw text
    content, via the SAME real parse :func:`scan_refs` uses. A destination that
    merely LOOKS like an embed/cross-reference (e.g. a report narrative
    documenting the markdown syntax itself inside backticks — `` `![x]
    (evidence/y.png)` ``) never becomes an ``image``/``link`` node in the first
    place (markdown-it never re-parses code content for inline syntax at all —
    see the module docstring), so it never appears in :func:`scan_refs`'s
    result; this is the other half a caller needs to tell such a lookalike
    apart from a real reference when it does its OWN, broader textual matching
    over the same markdown (:mod:`grison.engine.filesets`'s identity-folding,
    which must recognise both an embed and a cross-reference by shape alone,
    since the two forms differ only in a leading ``!``) — checking a match's
    own text for containment in one of these strings is a reliable, tokenizer-
    grounded "was this actually inside code" test, without a second hand-rolled
    matching regex of its own."""
    if not md.strip():
        return []
    normalized = md.replace("\r\n", "\n")
    tree = SyntaxTreeNode(_MD.parse(normalized))
    return [n.content for n in tree.walk() if n.type in ("code_inline", "code_block", "fence")]


def scan_refs(md: str) -> list[FoundRef]:
    """Every embed/cross-reference in ``md``, in document order."""
    if not md.strip():
        return []
    normalized = md.replace("\r\n", "\n")
    tree = SyntaxTreeNode(_MD.parse(normalized))

    standalone_ids: set[int] = set()
    out: list[FoundRef] = []

    def visit_block(node: SyntaxTreeNode, *, in_list_item: bool) -> None:
        if node.type == "paragraph":
            inline = node.children[0] if node.children else None
            kids = inline.children if inline else []
            if len(kids) == 1 and kids[0].type == "image":
                img = kids[0]
                standalone_ids.add(id(img.token))
                out.append(
                    _from_image(img, _line_of(node), standalone=True, in_list_item=in_list_item)
                )
        elif node.type in ("bullet_list", "ordered_list"):
            for li in node.children:
                if li.type != "list_item":
                    continue
                for child in li.children:
                    if child.type in ("bullet_list", "ordered_list"):
                        for sub_li in child.children:
                            for sub_child in sub_li.children:
                                visit_block(sub_child, in_list_item=True)
                    else:
                        visit_block(child, in_list_item=True)
        elif node.type == "blockquote":
            for child in node.children:
                visit_block(child, in_list_item=in_list_item)
        # fence / code_block / html_block / thematic_break / heading (as a
        # container): never hold a standalone embed candidate — nothing to do.

    for top in tree.children:
        visit_block(top, in_list_item=False)

    # Second pass: every image/link ANYWHERE (headings, mid-sentence, nested), so a
    # non-standalone image (REF-001) and every cross-reference link still get
    # reported — code fences/spans are structurally excluded already (see module
    # docstring), never walked into by markdown-it's own tree in the first place.
    for node in tree.walk():
        if node.type == "image" and id(node.token) not in standalone_ids:
            out.append(
                _from_image(
                    node, _nearest_line(node), standalone=False, in_list_item=_in_list_item(node)
                )
            )
        elif node.type == "link":
            out.append(
                FoundRef(
                    kind="cross_reference",
                    path=decode_ref_path(str(node.attrs.get("href") or "")),
                    caption=_flatten(node),
                    title=str(node.attrs.get("title") or ""),
                    line=_nearest_line(node),
                    standalone=False,
                    in_list_item=_in_list_item(node),
                )
            )

    out.sort(key=lambda r: r.line)
    return out


def _from_image(
    node: SyntaxTreeNode, line: int, *, standalone: bool, in_list_item: bool
) -> FoundRef:
    return FoundRef(
        kind="embed",
        path=decode_ref_path(str(node.attrs.get("src") or "")),
        caption=_flatten(node),
        title=str(node.attrs.get("title") or ""),
        line=line,
        standalone=standalone,
        in_list_item=in_list_item,
    )


def _flatten(node: SyntaxTreeNode) -> str:
    """Plain text of an image's alt / a link's visible text — markup (bold/em/code)
    stripped, matching what a reader actually sees."""
    if node.type in ("text", "code_inline"):
        return node.content
    if not node.children:
        return ""
    return "".join(_flatten(c) for c in node.children)


def _line_of(node: SyntaxTreeNode) -> int:
    return (node.map[0] + 1) if node.map else 0


def _nearest_line(node: SyntaxTreeNode) -> int:
    """Inline nodes (image/link) carry no ``.map`` of their own — walk up to the
    nearest block ancestor that does."""
    n: SyntaxTreeNode | None = node
    while n is not None:
        if n.map:
            return n.map[0] + 1
        n = n.parent
    return 0


def _in_list_item(node: SyntaxTreeNode) -> bool:
    n = node.parent
    while n is not None:
        if n.type == "list_item":
            return True
        n = n.parent
    return False


# --- ref_spans: exact source character spans, matched 1:1 with scan_refs() ---
#
# A caller that needs to fold a reference's markdown syntax IN PLACE (e.g.
# grison.engine.filesets, computing a canonical hash with each reference's
# destination replaced by its resolved remote identity) must splice at the
# reference's REAL position, never re-derive "what a reference looks like"
# with a second, hand-rolled matching regex over the whole document text: two
# lookalike occurrences of the same text (a real reference, and an unrelated
# copy of that same text sitting inside a code span/fence elsewhere) are
# otherwise indistinguishable to a regex that only sees TEXT, never POSITION.
#
# markdown-it-py's inline tokens carry no character offsets of their own (only
# a BLOCK's `.map` gives a line range), so this module computes offsets itself:
# every "paragraph"/heading node's `.map` gives its own exact source-line range
# (correct even nested inside a list item — confirmed against markdown-it-py's
# own tree), which slices out that block's raw text; within that slice, a
# small bracket-depth-and-escape-aware scanner (never a second copy of
# CommonMark's own link grammar — destination/title matching stays exactly as
# permissive as this module's own callers already relied on) finds every
# top-level `![...](...)`/`[...](...)` span, skipping anything inside an
# INLINE code span (a fenced/indented code block is a separate sibling node at
# the tree level, never inline-parsed at all, so it can never even reach this
# scan — the same reason a fenced copy of a real reference is already invisible
# to :func:`scan_refs` itself). Walking blocks in the same `tree.walk()` order
# :func:`scan_refs` sorts its own results by (block start line, then
# left-to-right within it) reproduces that exact same order, so the two lists
# pair up by plain ordinal index — never a second copy of "which kind is this."
#
# A count MISMATCH between the two (this scanner found a different number of
# spans than :func:`scan_refs` found real references) is the caller's signal
# that this scanner's simpler grammar disagreed with the real parser on some
# construct — today, that is exactly one shape: an image nested inside a
# link's text (``[![alt](inner)](outer)``), where :func:`scan_refs` reports
# TWO references (the nested embed and the outer cross-reference) for the ONE
# span this scanner finds (correctly, since CommonMark closes the link's own
# ``]`` at the OUTER bracket, treating the nested image as ordinary caption
# content). This module reports no opinion about that shape — it never
# validates or resolves (module docstring) — a caller sees the mismatch and
# decides what to do about it.


def _code_span_ranges(text: str) -> list[tuple[int, int]]:
    """Character-position ranges of every INLINE code span in one block's own
    raw text (never a fence/indented code block — those are separate sibling
    nodes, never inline-parsed, so their content never reaches here at all):
    a backtick run, then the next run of the SAME length that is not itself
    part of a longer run (CommonMark's own code-span closing rule) — a plain,
    non-regex scan so a run shorter/longer than the opener is correctly
    skipped rather than mismatched."""
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        run_len = j - i
        k = j
        closed_at: int | None = None
        while k < n:
            if text[k] != "`":
                k += 1
                continue
            k2 = k
            while k2 < n and text[k2] == "`":
                k2 += 1
            if k2 - k == run_len:
                closed_at = k2
                break
            k = k2
        if closed_at is None:
            i = j  # unmatched opener: not a code span, keep scanning past it
            continue
        ranges.append((i, closed_at))
        i = closed_at
    return ranges


def _code_range_end(pos: int, ranges: list[tuple[int, int]]) -> int | None:
    """The end of the code-span range strictly containing ``pos`` (its start
    included, so the scan below can jump straight past the whole span as one
    atomic unit — a bracket/backslash INSIDE a code span is literal content,
    never part of the reference grammar around it), or ``None``."""
    for a, b in ranges:
        if a <= pos < b:
            return b
    return None


_REF_DEST_STOP = frozenset(") \t\n\r\f\v")
_WHITESPACE = frozenset(" \t\n\r\f\v")


def _try_parse_ref(
    text: str, bracket_pos: int, code_ranges: list[tuple[int, int]]
) -> tuple[int, str] | None:
    """``text[bracket_pos] == "["``. Bracket-depth-and-escape-aware caption
    scan (the actual fix for a caption containing an escaped ``\\]`` or a
    nested ``[...]``/``![...]``, which a flat ``[^\\]]*`` regex can never
    match), then destination/title matching left exactly as permissive as
    this module's existing callers already relied on (a literal ``)`` or
    whitespace always ends the destination; not a defect in scope here).
    Returns ``(end position exclusive, raw caption text)`` or ``None`` if
    ``text`` at this position isn't a well-formed reference at all."""
    n = len(text)
    j = bracket_pos + 1
    depth = 1
    while j < n:
        skip_to = _code_range_end(j, code_ranges)
        if skip_to is not None:
            j = skip_to
            continue
        c = text[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0 or j >= n:
        return None
    caption_raw = text[bracket_pos + 1 : j]
    k = j + 1
    if k >= n or text[k] != "(":
        return None
    k += 1
    dest_start = k
    while k < n and text[k] not in _REF_DEST_STOP:
        k += 1
    if k == dest_start:
        return None  # empty destination — never a reference grison recognizes
    if k < n and text[k] != ")":
        m = k
        while m < n and text[m] in _WHITESPACE:
            m += 1
        if m < n and text[m] == '"':
            m += 1
            while m < n and text[m] != '"':
                m += 1
            if m >= n:
                return None
            m += 1
            while m < n and text[m] in _WHITESPACE:
                m += 1
        k = m
    if k >= n or text[k] != ")":
        return None
    return k + 1, caption_raw


def _scan_block_ref_spans(text: str) -> list[tuple[int, int]]:
    """Every top-level ``![...](...)``/``[...](...)`` span in one block's own
    raw text, in document order, local to ``text`` (see :func:`ref_spans` for
    conversion to global document offsets)."""
    code_ranges = _code_span_ranges(text)
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        skip_to = _code_range_end(i, code_ranges)
        if skip_to is not None:
            i = skip_to
            continue
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        is_bang = c == "!" and i + 1 < n and text[i + 1] == "["
        if is_bang or c == "[":
            bracket_pos = i + 1 if is_bang else i
            result = _try_parse_ref(text, bracket_pos, code_ranges)
            if result is not None:
                end_pos, _caption_raw = result
                spans.append((i, end_pos))
                i = end_pos
                continue
        i += 1
    return spans


def _line_offsets(text: str) -> list[int]:
    """Char offset of the start of each 0-based line in ``text`` — converts a
    block's ``.map`` (a line range) into character positions."""
    offsets = [0]
    for line in text.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


_REF_BEARING_BLOCK_TYPES = ("paragraph", "heading")


def ref_spans(md: str) -> list[tuple[int, int]]:
    """The character ``(start, end)`` span of every reference :func:`scan_refs`
    reports on the SAME ``md``, in the SAME order (``end`` is exclusive; the
    span covers the ENTIRE syntax, leading ``!`` included for an embed) — see
    the section docstring above for why a caller pairs these up with
    :func:`scan_refs`'s own result by plain ordinal index rather than
    re-deriving "which kind is this" itself, and what a count mismatch means."""
    if not md.strip():
        return []
    normalized = md.replace("\r\n", "\n")
    tree = SyntaxTreeNode(_MD.parse(normalized))
    offsets = _line_offsets(normalized)
    spans: list[tuple[int, int]] = []
    for node in tree.walk():
        if node.type not in _REF_BEARING_BLOCK_TYPES or not node.map:
            continue
        start_line, end_line = node.map
        block_start = offsets[start_line]
        block_end = offsets[end_line] if end_line < len(offsets) else len(normalized)
        block_text = normalized[block_start:block_end]
        spans.extend(
            (block_start + a, block_start + b) for a, b in _scan_block_ref_spans(block_text)
        )
    return spans
