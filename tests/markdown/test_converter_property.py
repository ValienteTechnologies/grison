"""Property tests (hypothesis) over the supported markdown grammar: escapes,
links with parens, nested emphasis, code spans with backticks, lists with
multi-block items, ordered lists with ``start``, embeds/cross-references through
a fake resolver, and template delimiters.

Three properties, per the brief:
  (a) ``md_to_html`` never raises on generated (supported) input.
  (b) ``html_to_md``'s own output is always an IMMEDIATE fixpoint: whatever it
      emits, one more push+pull round through it comes back byte-identical —
      never "close enough after one extra round". Going from AUTHOR markdown
      to that fixpoint may take one normalizing pass (grison canonicalizes
      some constructs — ``_em_`` -> ``*em*``, ordered-list renumbering, etc.),
      but from the fixpoint itself there is no further drift, checked two
      ways: starting from generated markdown
      (``test_html_to_md_of_md_to_html_reaches_fixpoint`` — r1 is already
      ``html_to_md`` output, and r2 must equal it exactly) and starting from
      generated HTML directly (``test_html_to_md_output_is_an_immediate_
      fixpoint`` — this is what actually caught real bugs in the old v1→v2 body
      migration the markdown-first generator never reached; that migration was
      later dropped entirely — D13 now just refuses an old-format workspace — but
      the property itself stayed the tighter, HTML-first regression check).
  (c) canonical HTML is stable: ``md_to_html(html_to_md(h)) == h`` for every ``h``
      produced by ``md_to_html`` in this run.

Kept fast (bounded examples, no per-test deadline) so it stays a few seconds in CI.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.markdown.refs import LocalRef, RemoteRef

# --- a small, fixed, fake resolver world (embed/cross-reference generation) ---

_KNOWN_EVIDENCE = {
    "evidence/shot1.png": (1, "shot1"),
    "evidence/shot2.png": (2, "shot2"),
}


@dataclass
class _FakeResolver:
    def to_remote(self, path: str) -> RemoteRef | None:
        entry = _KNOWN_EVIDENCE.get(path)
        if entry is None:
            return None
        ev_id, name = entry
        return RemoteRef("gw-evidence", ev_id, name, None)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        for path, (ev_id, name) in _KNOWN_EVIDENCE.items():
            if (remote.id is not None and remote.id == ev_id) or (
                remote.name is not None and remote.name == name
            ):
                return LocalRef(path=path, caption=name.capitalize())
        return None


RESOLVER = _FakeResolver()

WORDS = [
    "foo",
    "bar_baz",
    "user_id",
    "CVE-2021-1234",
    "cafe",
    "quick",
    "session_token",
]
URLS = [
    "http://example.com/x",
    "http://example.com/path(1)",
    "http://example.com/x?y=1",
]
EVIDENCE_PATHS = list(_KNOWN_EVIDENCE)


@st.composite
def inline_fragment(draw: st.DrawFn) -> str:
    kind = draw(
        st.sampled_from(
            [
                "word",
                "bold",
                "em",
                "strongem",
                "code",
                "code_with_backtick",
                "link",
                "cross_ref",
                "escaped",
                "template_literal",
                "template_active",
                "raw_literal_text",
            ]
        )
    )
    word = draw(st.sampled_from(WORDS))
    if kind == "word":
        return word
    if kind == "bold":
        return f"**{word}**"
    if kind == "em":
        return f"*{word}*"
    if kind == "strongem":
        return f"***{word}***"
    if kind == "code":
        return f"`{word}`"
    if kind == "code_with_backtick":
        return f"``a`{word}``"
    if kind == "link":
        url = draw(st.sampled_from(URLS))
        return f"[{word}]({url})"
    if kind == "cross_ref":
        path = draw(st.sampled_from(EVIDENCE_PATHS))
        return f"[{word}]({path})"
    if kind == "escaped":
        return rf"\*{word}\*"
    if kind == "template_active":
        # the reserved gw: form — an ACTIVE (un-escaped) Jinja expression the
        # author deliberately wrote as such (see module docstring).
        return "`gw:{{ " + word + " }}`"
    if kind == "raw_literal_text":
        # author text that happens to SPELL "{% raw %}"/"{% endraw %}" with no
        # jinja intent at all — must round-trip as ordinary literal text, never
        # specially interpreted (there is no markdown "raw block" authoring
        # syntax at all; D10 escaping neutralizes these words' own braces like
        # any other literal delimiter tokens — see
        # test_literal_raw_endraw_author_text_not_eaten_by_own_unwrap).
        return "{% raw %} " + word + " {% endraw %}"
    return draw(st.sampled_from(["{{ x }}", "{% if x %}", "{# a comment #}"]))


@st.composite
def paragraph_text(draw: st.DrawFn) -> str:
    n = draw(st.integers(min_value=1, max_value=4))
    return " ".join(draw(inline_fragment()) for _ in range(n))


@st.composite
def embed_line(draw: st.DrawFn) -> str:
    path = draw(st.sampled_from(EVIDENCE_PATHS))
    caption = draw(st.sampled_from(WORDS))
    return f"![{caption}]({path})"


@st.composite
def list_block(draw: st.DrawFn) -> str:
    is_ol = draw(st.booleans())
    # A marker/delimiter change starts a NEW list under real CommonMark, so the
    # bullet char and ordered delimiter are each drawn ONCE per list, not per item.
    bullet_char = draw(st.sampled_from(["-", "*", "+"]))
    ol_delim = draw(st.sampled_from([".", ")"]))
    start = draw(st.integers(min_value=1, max_value=4)) if is_ol else 1
    n_items = draw(st.integers(min_value=1, max_value=3))
    lines: list[str] = []
    num = start
    for _ in range(n_items):
        marker = f"{num}{ol_delim} " if is_ol else f"{bullet_char} "
        head = draw(paragraph_text())
        lines.append(marker + head)
        if is_ol:
            num += 1
        # Real CommonMark requires a continuation/nested block's indent to match
        # (at least) THIS item's own marker width to belong to it.
        indent = " " * len(marker)
        if draw(st.booleans()) and "\n" not in head:  # lazy continuation (no indent at all)
            lines.append(draw(paragraph_text()))
        if draw(st.booleans()):  # loose continuation block for this item
            lines.append("")
            continuation_kind = draw(st.sampled_from(["embed", "para", "fence"]))
            if continuation_kind == "embed":
                lines.append(indent + draw(embed_line()))
            elif continuation_kind == "fence":
                # A fence's own physical lines get NO CommonMark laziness (unlike
                # a plain paragraph's hard-break continuation) — every one needs
                # this item's own marker-width indent to still belong to it.
                lines.extend(indent + ln for ln in draw(fence_text()).split("\n"))
            else:
                lines.append(indent + draw(paragraph_text()))
        if draw(st.booleans()):  # one nested sub-list, one level
            nested_ol = draw(st.booleans())
            nested_bullet = draw(st.sampled_from(["-", "*", "+"]))
            nested_delim = draw(st.sampled_from([".", ")"]))
            # An ordered nested list can only interrupt the preceding paragraph
            # (no blank line before it, the shape here) if it starts at 1.
            nested_num = 1
            for _ in range(draw(st.integers(min_value=1, max_value=2))):
                nested_marker = f"{nested_num}{nested_delim} " if nested_ol else f"{nested_bullet} "
                lines.append(indent + nested_marker + draw(paragraph_text()))
                if nested_ol:
                    nested_num += 1
    return "\n".join(lines)


@st.composite
def fence_text(draw: st.DrawFn) -> str:
    # CommonMark treats a tilde fence identically to a backtick one (same
    # "fence" node) — grison accepts either on authoring, always
    # re-canonicalizing to backticks on the html->markdown side.
    marker = draw(st.sampled_from(["```", "~~~", "````"]))
    lang = draw(st.sampled_from(["", "bash", "python", "json"]))
    header = f"{marker}{lang}" if lang else marker
    n_lines = draw(st.integers(min_value=1, max_value=2))
    body_lines = [draw(st.sampled_from(WORDS)) for _ in range(n_lines)]
    return "\n".join([header, *body_lines, marker])


@st.composite
def blockquote_block(draw: st.DrawFn) -> str:
    n_paras = draw(st.integers(min_value=1, max_value=2))
    segments = [draw(paragraph_text()) for _ in range(n_paras)]
    if draw(st.booleans()):
        # A small, single-level list — enough to exercise "blockquote
        # containing a list" without the full list_block() composite's own
        # nested-sublist/loose-continuation complexity.
        bullet = draw(st.sampled_from(["-", "*", "+"]))
        n_items = draw(st.integers(min_value=1, max_value=2))
        segments.append("\n".join(f"{bullet} {draw(paragraph_text())}" for _ in range(n_items)))
    lines: list[str] = []
    for i, seg in enumerate(segments):
        if i > 0:
            lines.append(">")
        lines.extend(f"> {ln}" if ln else ">" for ln in seg.split("\n"))
    return "\n".join(lines)


@st.composite
def table_block(draw: st.DrawFn) -> str:
    ncols = draw(st.integers(min_value=1, max_value=3))
    header = [draw(st.sampled_from(WORDS)) for _ in range(ncols)]
    nrows = draw(st.integers(min_value=0, max_value=2))
    rows = [[draw(inline_fragment()) for _ in range(ncols)] for _ in range(nrows)]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
        *("| " + " | ".join(r) + " |" for r in rows),
    ]
    return "\n".join(lines)


@st.composite
def document(draw: st.DrawFn) -> str:
    # Real CommonMark merges two adjacent lists separated by only a blank line
    # into ONE (loose) list rather than keeping them separate — never generate
    # two list blocks back to back (matches what a real CommonMark renderer, or
    # grison's own parser, actually does with such input).
    n = draw(st.integers(min_value=1, max_value=3))
    blocks = []
    prev_was_list = False
    for _ in range(n):
        choices = (
            ["para", "embed", "fence", "blockquote", "table"]
            if prev_was_list
            else ["para", "list", "embed", "fence", "blockquote", "table"]
        )
        kind = draw(st.sampled_from(choices))
        if kind == "para":
            blocks.append(draw(paragraph_text()))
        elif kind == "list":
            blocks.append(draw(list_block()))
        elif kind == "fence":
            blocks.append(draw(fence_text()))
        elif kind == "blockquote":
            blocks.append(draw(blockquote_block()))
        elif kind == "table":
            blocks.append(draw(table_block()))
        else:
            blocks.append(draw(embed_line()))
        prev_was_list = kind == "list"
    return "\n\n".join(blocks)


_SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@_SETTINGS
@given(document())
def test_md_to_html_never_raises_on_supported_grammar(doc: str) -> None:
    md_to_html(doc, refs=RESOLVER)


@_SETTINGS
@given(document())
def test_html_to_md_of_md_to_html_reaches_fixpoint(doc: str) -> None:
    """r1 (``html_to_md`` output) is checked as an IMMEDIATE fixpoint — r2
    must equal r1 exactly, not merely "close" after one more round. Only the
    doc -> r1 step is allowed to normalize; r1 -> r2 must not move at all,
    for every generated author markdown document."""
    try:
        html = md_to_html(doc, refs=RESOLVER)
    except ConverterError:
        return  # outside the supported grammar for this generator run — not our concern here
    r1 = html_to_md(html, refs=RESOLVER)
    r2 = html_to_md(md_to_html(r1, refs=RESOLVER), refs=RESOLVER)
    assert r2 == r1, f"non-fixpoint: r1={r1!r} r2={r2!r}"


@_SETTINGS
@given(document())
def test_canonical_html_is_stable(doc: str) -> None:
    try:
        html = md_to_html(doc, refs=RESOLVER)
    except ConverterError:
        return
    md_back = html_to_md(html, refs=RESOLVER)
    assert md_to_html(md_back, refs=RESOLVER) == html


# --- html_to_md must never emit markdown its own md_to_html reads differently ---
# Generates real GW-shaped rich-text HTML (strong/em/code/a/span, leading/
# trailing/only whitespace inside them, adjacent inline elements with no space
# between, nested strong+em, text with *, _, <, >, [, ], backticks, backslashes)
# and checks the VISIBLE TEXT survives html_to_md -> md_to_html unchanged, and
# that markup (strong stays strong, etc.) does too.

from html.parser import HTMLParser  # noqa: E402


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.tags.add(tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_text(html: str) -> str:
    """What a reader actually sees: HTML collapses/strips insignificant
    whitespace at render time (leading/trailing runs, internal runs collapsed to
    one space) — a real browser (or GW's own TipTap editor / docx export) never
    shows a difference between e.g. "<p>x </p>" and "<p>x</p>", so this
    normalizes the same way rather than doing a byte-exact text extraction."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    return " ".join(text.split())


def _tags_used(html: str) -> set[str]:
    # "p" wrapping is a documented normalization (a v1-era normalization, kept);
    # "span" is BY DESIGN always unwrapped/dropped (cosmetic-only — see the
    # converter's module docstring), never a round-trip failure.
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return p.tags - {"p", "span"}


def _strip_degenerate_empty_tags(html: str) -> str:
    """Formatting around EMPTY-OR-WHITESPACE-ONLY content (``<strong> </strong>``,
    or ``<strong><span> </span></strong>`` once span unwraps to the same thing)
    has no CommonMark representation at all — there's no non-whitespace content
    for a delimiter to flank — and no visual meaning either (bold whitespace
    looks identical to plain whitespace to any reader), so grison correctly
    drops the tag rather than the impossible alternative of inventing content.
    Strips such tags (innermost first, to fixpoint, so nested degenerate wrapping
    collapses too) before comparing "markup survives" — this is EXPECTED loss
    for content that carried no information, not a round-trip failure."""
    prev = None
    while prev != html:
        prev = html
        html = _EMPTY_ELEMENT_RE.sub("", html)
    return html


def _esc_html_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TRICKY_WORDS = [
    "plain",
    "",
    " ",
    "   ",
    "  leading",
    "trailing  ",
    " both ",
    "*starred*text",
    "_under_scored_",
    "<tag>looking",
    "a<b>c",
    "back`tick`text",
    "back\\slash\\text",
    "amp&ersand",
    "[bracket]text",
    "para(gram)text",
    "zero​width",  # an author's OWN literal Cf character — must survive, not multiply
]

# A word that STARTS or ENDS with any markdown/HTML-significant character
# (grison's own _MD_ESCAPE_CHARS set — "\\`*_[]<&!" — replicated here) is
# excluded from being the sole/leaf content of a <strong>/<em> specifically:
# once escaped, such a leaf renders with punctuation (its escaping backslash,
# or the character itself) immediately touching the wrapper's own delimiter —
# a genuinely deeper CommonMark flanking-rule interaction than the
# same-tag SIBLING merge `_merge_adjacent_inline` handles (that fixes two
# ADJACENT same-tag elements colliding into one ambiguous delimiter run; this
# is about a delimiter run flanked by a non-whitespace/non-punctuation character on one
# side and punctuation on the other isn't flanking on either side, so it can't
# open/close at all). This is a genuine, narrow markdown-representability gap,
# not something a real Ghostwriter/TinyMCE edit produces (there's no toolbar
# path to bold text starting with a literal asterisk with nothing separating
# it from the bold delimiter itself) — the generator avoids constructing it;
# text (including these tricky words) is still fully exercised standalone,
# unwrapped, via the plain "text" kind.
_ESCAPE_ADJACENT_CHARS = set("\\`*_[]<&!")
SAFE_LEAF_WORDS = [
    w
    for w in TRICKY_WORDS
    if not (w and w[0] in _ESCAPE_ADJACENT_CHARS) and not (w and w[-1] in _ESCAPE_ADJACENT_CHARS)
]


@st.composite
def gw_inline_html(
    draw: st.DrawFn,
    depth: int = 0,
    exclude: frozenset[str] = frozenset(),
    safe_leaf: bool = False,
) -> str:
    """``exclude`` keeps ``<strong>``/``<em>`` from directly nesting a construct
    that starts/ends in the SAME markdown delimiter character with nothing
    between them and the wrapper's own delimiter — real CommonMark can't
    round-trip that combination as written (e.g. wrapping a bare code span
    directly in strong produces `` **`x`** ``, which — immediately adjacent to a
    PRECEDING word character with no space, as a sibling fragment often is —
    fails CommonMark's flanking rule for a totally different, deeper reason than
    what `_merge_adjacent_inline` handles (two ADJACENT same-tag SIBLINGS
    merging into one element): a delimiter run flanked by a word character on one side and
    punctuation (the code span's own backtick) on the other isn't flanking on
    EITHER side, so it can't open/close at all. This is a genuine, narrow
    markdown-representability gap for that specific double-degenerate nesting —
    not something a real Ghostwriter/TinyMCE editor produces (there's no toolbar
    path to nest bold directly around nothing-but-a-code-span, or bold-in-bold) —
    so the generator avoids constructing it rather than this property chasing an
    edge no real content hits. ``safe_leaf`` applies the SAME reasoning to plain
    text chosen as a <strong>/<em>'s own leaf content — see SAFE_LEAF_WORDS."""
    words = SAFE_LEAF_WORDS if safe_leaf else TRICKY_WORDS
    if depth >= 2 or safe_leaf:
        # safe_leaf content stops here regardless of depth: a <strong>/<em>'s
        # DIRECT content is always plain text, never another structured
        # construct (a link, code span, or further nesting) — every one of
        # those starts with its OWN punctuation ("[" for a link, "`" for code),
        # which hits the exact same flanking-rule gap SAFE_LEAF_WORDS exists to
        # avoid, just one level removed. "code"/"a"/nesting under strong/em are
        # still fully exercised — just each independently (see the "code"/"a"
        # cases below, and nested strong-in-em/em-in-strong is already proven
        # safe by existing pinned tests, e.g. test_converter_fixpoint.py's
        # "nested_em_in_strong"/"nested_strong_in_em" cases), not glued
        # zero-space against a sibling the way adjacent_strong_em tests.
        return _esc_html_text(draw(st.sampled_from(words)))
    choices = ["text", "strong", "em", "code", "a", "span", "adjacent_strong_em"]
    choices = [c for c in choices if c not in exclude]
    kind = draw(st.sampled_from(choices))
    if kind == "text":
        return _esc_html_text(draw(st.sampled_from(words)))
    if kind == "strong":
        inner = draw(
            gw_inline_html(
                depth + 1,
                exclude=frozenset({"strong", "code", "adjacent_strong_em"}),
                safe_leaf=True,
            )
        )
        return f"<strong>{inner}</strong>"
    if kind == "em":
        inner = draw(
            gw_inline_html(
                depth + 1,
                exclude=frozenset({"em", "code", "adjacent_strong_em"}),
                safe_leaf=True,
            )
        )
        return f"<em>{inner}</em>"
    if kind == "code":
        return f"<code>{_esc_html_text(draw(st.sampled_from(TRICKY_WORDS)))}</code>"
    if kind == "a":
        # A link can't validly contain another link (neither real HTML nor
        # CommonMark supports nested anchors — no real editor produces this).
        inner = draw(gw_inline_html(depth + 1, exclude=frozenset({"a"})))
        return f'<a href="http://example.com/x" target="_blank" rel="noopener">{inner}</a>'
    if kind == "span":
        return f"<span>{draw(gw_inline_html(depth + 1))}</span>"
    # adjacent, no space between them at all — strong immediately followed by em
    # (DIFFERENT delimiter characters: "**" vs "*") is the one directly-adjacent
    # combination that's actually safe and unambiguous, verified separately. Each
    # side excludes its OWN tag (a becomes <strong>'s content, b becomes <em>'s)
    # for the same same-tag-nesting reason as the "strong"/"em" cases above.
    a = draw(gw_inline_html(depth + 1, exclude=frozenset({"code", "strong"}), safe_leaf=True))
    b = draw(gw_inline_html(depth + 1, exclude=frozenset({"code", "em"}), safe_leaf=True))
    return f"<strong>{a}</strong><em>{b}</em>"


# "a" is deliberately excluded: an empty link `[](url)` IS valid, round-tripping
# CommonMark (unlike empty strong/em/code/span, which have no representation at
# all for "wrap nothing") — grison's link rendering doesn't special-case empty
# text, and correctly doesn't need to. "code" only matches when TRULY empty
# (zero characters) — unlike strong/em, CommonMark's code-span rule keeps
# whitespace-only content as-is (`` ` ` `` is a valid one-space code span), so
# only genuinely empty <code></code> (no representation at all — see
# _fence_code) is degenerate.
_EMPTY_ELEMENT_RE = re.compile(r"<(strong|em|span)[^>]*>\s*</\1>|<code[^>]*></code>")


@st.composite
def gw_paragraph_html(draw: st.DrawFn) -> str:
    n = draw(st.integers(min_value=1, max_value=4))
    # Top-level fragments join with a real space between them — true zero-space
    # adjacency between two ARBITRARY constructs is a separate, narrower
    # question (does CommonMark's flanking rule even allow representing text
    # ending in a word character directly glued, with no space, to a following
    # strong/em run that starts with punctuation? — often NOT: this is a genuine
    # CommonMark representability limit, not a grison bug, and no real
    # Ghostwriter/TinyMCE edit produces that exact glued shape either). Adjacent
    # elements WITH no space between them, the combination that IS safe and that
    # the brief asks for, is covered by the dedicated "adjacent_strong_em" kind
    # inside gw_inline_html (fixed, verified-safe delimiter characters).
    inner = " ".join(draw(gw_inline_html()) for _ in range(n))
    # A whitespace-only (or empty) OVERALL paragraph is a degenerate generator
    # artifact, not real content — both converters treat all-whitespace input as
    # "empty" via their own top-level shortcuts, an intentional, unrelated
    # simplification (see md_to_html's docstring), not this property's concern.
    # Individual EMPTY-OR-WHITESPACE-ONLY inline elements (<strong> </strong>)
    # are deliberately still generated and exercised — see
    # _strip_degenerate_empty_tags for why "markup survives" doesn't apply to
    # them (there's genuinely nothing there for markdown to represent).
    if not inner.strip():
        inner = "x" + inner
    return f"<p>{inner}</p>"


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw_paragraph_html())
def test_html_to_md_output_reads_back_with_same_visible_text_and_markup(html: str) -> None:
    try:
        md = html_to_md(html)
    except ConverterError:
        return  # not this property's concern — html_to_md itself rejecting is fine
    html2 = md_to_html(md)
    assert _visible_text(html2) == _visible_text(html), (
        f"visible text changed: html={html!r} md={md!r} html2={html2!r}"
    )
    expected_tags = _tags_used(_strip_degenerate_empty_tags(html))
    assert _tags_used(html2) == expected_tags, (
        f"markup changed: html={html!r} md={md!r} html2={html2!r}"
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw_paragraph_html())
def test_html_to_md_output_is_an_immediate_fixpoint(html: str) -> None:
    """Stronger than the visible-text/markup property above: whatever
    ``html_to_md`` emits for ARBITRARY (not just author-written) HTML must be a
    fixpoint the FIRST time — not "after at most one extra round" — checked as
    EXACT markdown equality, not just visible text. ``test_html_to_md_of_md_to_
    html_reaches_fixpoint`` below tests the same thing starting from AUTHOR
    markdown; this one starts from HTML directly, which is what caught real bugs
    in the old (now-dropped) v1→v2 body migration the markdown-first generator
    didn't reach."""
    try:
        m = html_to_md(html)
    except ConverterError:
        return
    m2 = html_to_md(md_to_html(m))
    assert m2 == m, f"non-fixpoint: html={html!r} m={m!r} m2={m2!r}"


# --- defect fix: trailing empty/&nbsp;-only top-level <p> blocks (real TipTap
# editor artifact — the exact seed was Ghostwriter 7.2.6's own "Missing HTTP
# Security Headers" library finding, see tests/e2e/test_findings_e2e.py's
# ``test_nbsp_padded_trailing_paragraphs_pull_clean_with_no_settle_push``). Mixes
# a real content paragraph with genuinely-empty and whitespace-only (plain space,
# tab, and non-breaking space) sibling <p> blocks, at every position.

_PADDING_BLOCK_BODIES = ["", " ", "  ", "\t", "\xa0", "\xa0\xa0\xa0", " \xa0 "]


@st.composite
def top_level_blocks_with_empty_padding(draw: st.DrawFn) -> str:
    n_real = draw(st.integers(min_value=1, max_value=2))
    n_padding = draw(st.integers(min_value=1, max_value=3))
    blocks = [f"<p>{draw(st.sampled_from(WORDS))}</p>" for _ in range(n_real)]
    blocks += [f"<p>{draw(st.sampled_from(_PADDING_BLOCK_BODIES))}</p>" for _ in range(n_padding)]
    draw(st.randoms()).shuffle(blocks)
    return "".join(blocks)


@_SETTINGS
@given(top_level_blocks_with_empty_padding())
def test_empty_and_nbsp_padded_blocks_reach_a_stable_canonical_form(html: str) -> None:
    """The core of the fix: whatever ``html_to_md`` emits for this shape must
    already be at the canonical form ``md_to_html`` will reduce it to on the
    NEXT round anyway — never "close, but one more settle push needed" (the
    exact bug: a fresh pull's local file, once dumped/reparsed, disagreed with
    ``canonical_remote_prose``'s un-normalized ``html_to_md`` output)."""
    md = html_to_md(html)
    html2 = md_to_html(md)
    # canonical stability from the FIRST html_to_md call already — not merely
    # after an extra round trip.
    assert md_to_html(html_to_md(html2)) == html2
    assert html_to_md(html2) == md


def _paragraph_texts(html: str) -> list[str]:
    """Per-top-level-``<p>`` visible text, empty/whitespace-only (NBSP included —
    see ``_visible_text``) ones dropped: models what a reader actually sees from
    a sequence of BLOCK elements, where — unlike ``_visible_text``'s own
    adjacent-INLINE-element assumption — missing whitespace in the raw HTML
    SOURCE between two top-level ``<p>`` tags carries no visual meaning at all (a
    browser always renders each block on its own line regardless)."""
    return [t for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL) if (t := _visible_text(p))]


@_SETTINGS
@given(top_level_blocks_with_empty_padding())
def test_empty_and_nbsp_padded_blocks_keep_real_visible_text(html: str) -> None:
    md = html_to_md(html)
    assert _paragraph_texts(md_to_html(md)) == _paragraph_texts(html)


# --- neither direction ever invents/multiplies an invisible (Cf) character ---
# The old zero-width-space disambiguation mechanism (removed — see
# _merge_adjacent_inline) would have failed this outright, since it wrote
# U+200B into markdown that would then be pushed straight to Ghostwriter, and
# into files the validator would flag as a hygiene violation. Adjacent-element
# ambiguity is now resolved by merging the SOURCE tree instead (a real
# normalization, reported via on_loss), which introduces no characters at all.


def _cf_chars(s: str) -> Counter[str]:
    """Count Unicode category "Cf" (Format) characters — this covers every
    character the brief calls out by name (U+200B-U+200F, U+2060, U+FEFF are
    all Cf) plus any other invisible format character, in one check."""
    return Counter(c for c in s if unicodedata.category(c) == "Cf")


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw_paragraph_html())
def test_html_to_md_never_invents_or_multiplies_invisible_characters(html: str) -> None:
    try:
        md = html_to_md(html)
    except ConverterError:
        return
    input_cf = _cf_chars(html)
    output_cf = _cf_chars(md)
    assert output_cf == input_cf, (
        f"html_to_md changed invisible-character counts: "
        f"input={input_cf} output={output_cf} (html={html!r}, md={md!r})"
    )


_CF_SAFE_TEMPLATES = [
    lambda w: w,
    lambda w: f"**{w}**",
    lambda w: f"*{w}*",
    lambda w: f"***{w}***",
    lambda w: f"`{w}`",
    lambda w: f"[{w}](http://example.com/x)",  # an ORDINARY link — text IS preserved
    lambda w: f"plain {w} text",
]


@st.composite
def cf_bearing_paragraph(draw: st.DrawFn) -> str:
    """A plain paragraph built ONLY from constructs whose visible text is
    always preserved verbatim (never an embed caption or a cross-reference's
    synthesized link text — both are intentionally NOT stored in the HTML at
    all, per D1, unrelated to invisible-character handling and not this
    property's concern), sometimes containing a real Cf character the author
    typed themselves — it must survive, not be dropped or duplicated."""
    word = draw(st.sampled_from(WORDS + ["zero​width", "joined⁠text"]))
    template = draw(st.sampled_from(_CF_SAFE_TEMPLATES))
    return template(word)


@_SETTINGS
@given(cf_bearing_paragraph())
def test_md_to_html_never_invents_or_multiplies_invisible_characters(doc: str) -> None:
    try:
        html = md_to_html(doc)
    except ConverterError:
        return
    input_cf = _cf_chars(doc)
    output_cf = _cf_chars(html)
    assert output_cf == input_cf, (
        f"md_to_html changed invisible-character counts: "
        f"input={input_cf} output={output_cf} (doc={doc!r}, html={html!r})"
    )


# --- D10 defect fix: {% raw %}...{% endraw %} regions in STORED html ------------
# ``inline_fragment`` above already mixes literal delimiters, ``gw:`` active
# references, code spans and literal "raw"/"endraw" author TEXT into every
# existing property in this file (fixpoint, canonical stability, never-raises).
# This section adds the one shape those can't reach: a REAL, unescaped Jinja
# ``{% raw %}...{% endraw %}`` region actually present in HTML — never emitted by
# grison's own push any more, but real/legacy stored data can still carry one (see
# ``grison.markdown.converter``'s ``_RawScanState``/``_split_raw_regions``) — mixed
# with plain text and
# genuinely active ``{{ }}`` expressions sitting OUTSIDE it in the same field.

_RAW_INNER_SNIPPETS = [
    "{{7*7}}",
    "{% debug %}",
    "{{7*7}} and {% debug %}",
    "{{ {% }}",  # nested/overlapping delimiters, still just literal inside raw
    "plain text, no delimiters at all",
]


@st.composite
def html_with_raw_blocks(draw: st.DrawFn) -> str:
    n = draw(st.integers(min_value=1, max_value=3))
    parts: list[str] = []
    for _ in range(n):
        kind = draw(st.sampled_from(["plain", "raw", "active"]))
        word = draw(st.sampled_from(WORDS))
        if kind == "plain":
            parts.append(_esc_html_text(word))
        elif kind == "raw":
            inner = draw(st.sampled_from(_RAW_INNER_SNIPPETS))
            parts.append(f"{{% raw %}}{inner}{{% endraw %}}")
        else:
            parts.append("{{ " + word + " }}")
    return "<p>" + " ".join(parts) + "</p>"


@_SETTINGS
@given(html_with_raw_blocks())
def test_raw_block_resolves_to_literal_text_with_no_raw_markers_left(html: str) -> None:
    md = html_to_md(html)
    # the {% raw %}/{% endraw %} markers themselves are fully consumed — each
    # generated raw fragment is a complete, self-contained pair, so neither
    # word can survive as literal text in the markdown (D10: a raw region means
    # "this text is literal", not "keep the wrapper too").
    assert "{% raw %}" not in md
    assert "{% endraw %}" not in md


@_SETTINGS
@given(html_with_raw_blocks())
def test_raw_block_html_to_md_reaches_immediate_fixpoint(html: str) -> None:
    md = html_to_md(html)
    md2 = html_to_md(md_to_html(md))
    assert md2 == md, f"non-fixpoint: html={html!r} md={md!r} md2={md2!r}"


@_SETTINGS
@given(html_with_raw_blocks())
def test_raw_block_repush_never_nests_escaping(html: str) -> None:
    """The escaping mechanism is never nested (D10): once pulled and re-pushed,
    the HTML never contains a Jinja string-literal quoting expression
    (``{{ '...' }}``) INSIDE a ``{% raw %}...{% endraw %}`` block — because
    grison's push never emits ``{% raw %}`` at all any more (the per-token
    mechanism proven in
    the rework lab proof (``proofs/d10-jinja-escape-lab.md``, kept outside this repo) section 9
    replaced it), so the two can never co-occur, structurally."""
    md = html_to_md(html)
    html2 = md_to_html(md)
    assert "{% raw %}" not in html2
    assert "{% endraw %}" not in html2


# --- item 2 (fix-f): a raw region split across an inline tag, or across two
# top-level blocks, must be recognized as ONE region (see converter.py's
# _RawScanState/_split_raw_regions and the module docstring) — never two
# dangling `gw:` active markers, one per half.

_INLINE_TAGS_FOR_RAW_SPLIT = ["strong", "em", "code", "a", None]


@st.composite
def html_with_raw_region_split(draw: st.DrawFn) -> str:
    """One ``{% raw %}...{% endraw %}`` region whose opener and closer are
    separated either by a random inline tag wrapping the region's own content
    (inside one ``<p>``), or by a top-level block boundary (the opener's ``<p>``
    ends, the closer starts the next ``<p>``) — the two shapes the old
    per-text-run scan could never see across."""
    inner = draw(st.sampled_from(_RAW_INNER_SNIPPETS))
    lead = _esc_html_text(draw(st.sampled_from(WORDS)))
    trail = _esc_html_text(draw(st.sampled_from(WORDS)))
    tag = draw(st.sampled_from(_INLINE_TAGS_FOR_RAW_SPLIT))
    across_block = draw(st.booleans())
    wrapped_inner = (
        inner
        if tag is None
        else (
            f'<a href="http://example.com/x">{inner}</a>'
            if tag == "a"
            else f"<{tag}>{inner}</{tag}>"
        )
    )
    if across_block:
        return f"<p>{lead} {{% raw %}}{wrapped_inner}</p><p>{{% endraw %}} {trail}</p>"
    return f"<p>{lead} {{% raw %}}{wrapped_inner}{{% endraw %}} {trail}</p>"


@_SETTINGS
@given(html_with_raw_region_split())
def test_raw_region_split_across_tag_or_block_never_leaves_a_gw_marker(html: str) -> None:
    md = html_to_md(html)
    assert "{% raw %}" not in md
    assert "{% endraw %}" not in md
    assert "gw:" not in md, f"content inside the raw region became an active marker: {md!r}"


@_SETTINGS
@given(html_with_raw_region_split())
def test_raw_region_split_across_tag_or_block_reaches_immediate_fixpoint(html: str) -> None:
    md = html_to_md(html)
    md2 = html_to_md(md_to_html(md))
    assert md2 == md, f"non-fixpoint: html={html!r} md={md!r} md2={md2!r}"
