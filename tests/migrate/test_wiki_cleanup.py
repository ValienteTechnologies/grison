"""Tests for the one-time legacy-wiki cleanup tool (D5,
:mod:`grison.migrate.wiki_cleanup`).

The validator that will eventually enforce the new wiki-body rules is being
written in a different worktree and isn't available here (per the task), so
every "BEFORE violates the rule" assertion below is expressed with small,
independent checks built directly on markdown-it-py tokens and regexes — kept
in this file, deliberately NOT importing wiki_cleanup's own internals for the
oracle side of each proof (that would make the proof circular). The
corruption-artifact checks are the one exception that's supposed to overlap:
they use ``_ARTIFACT_RES`` below, a frozen copy of the patterns the removed
pre-engine sync module used to block pushes, per the task's instruction to
"read the two existing patterns ... and reproduce fixtures for them".
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from markdown_it import MarkdownIt

from grison.migrate.wiki_cleanup import (
    R_ARTIFACT,
    R_ARTIFACT_HEADING,
    R_BARE_LINK,
    R_BLANK_LINES,
    R_BLOCK_DRIFT,
    R_CONTROL_CHAR,
    R_CRLF,
    R_FILE_LINK,
    R_FINAL_NEWLINE,
    R_GALLERY_IMAGE,
    R_HTML_INLINE,
    R_HTML_TABLE,
    R_NBSP,
    R_TITLE_H1,
    R_TRAILING_WS,
    Change,
    CleanResult,
    Issue,
    clean_page,
    render_review,
    visible_text,
)

# Frozen copy of the artifact patterns the pre-engine BookStack sync module
# (``grison/remote/methodology.py``, removed when the wiki moved onto the engine) used to
# block pushes. Kept here as an independent oracle: the cleanup tool must repair exactly
# what that module used to flag.
_ARTIFACT_RES = [
    (re.compile(r"\]\(https?://[^)\s]*$", re.M), "truncated link", False),
    (re.compile(r'<span class="?citation'), "leaked citation span", True),
    (re.compile(r'<div class="?notice'), "leaked notice-block div", True),
]

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wiki-cleanup"
_MD = MarkdownIt("commonmark")
# markdown-it-py's own BLOCK-level reference-definition rule consults
# validateLink too (confirmed empirically): with the DEFAULT validator, a
# `[ref]: file:///...` definition is REJECTED outright and falls through to
# being read as ordinary paragraph text instead of a (correctly invisible)
# reference definition — which would make this file's own independent block-
# type-sequence oracle (test_block_type_sequence_preserved_property_on_
# fixtures_without_raw_html) see a bogus extra paragraph for every file:
# reference-style fixture. Overridden the same way wiki_cleanup._MD is (see
# that module's own docstring for why) — this stays a SEPARATE instance, not
# an import of the module under test, so the oracle is still independent.
_MD.validateLink = lambda url: True  # type: ignore[method-assign]

# --- independent "violates the new rule" oracles ----------------------------
#
# A small, fixed sample of real HTML element names — deliberately NOT
# wiki_cleanup's own _KNOWN_HTML_TAGS list, so a change to that list can't
# silently make these proofs meaningless.
_SAMPLE_KNOWN_TAGS = {
    "table", "thead", "tbody", "tr", "td", "th", "span", "div", "sup", "br",
    "p", "strong", "b", "em", "i", "code", "a", "img", "iframe", "script",
}  # fmt: skip

_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


def _flatten(tokens: Sequence[object]) -> list[object]:
    out: list[object] = []
    for t in tokens:
        out.append(t)
        children = getattr(t, "children", None)
        if children:
            out.extend(children)
    return out


def violates_raw_html(md: str) -> bool:
    """True if a real (non-placeholder) HTML tag appears as an html_block or
    html_inline token anywhere in ``md``."""
    for tok in _flatten(_MD.parse(md)):
        ttype = getattr(tok, "type", "")
        if ttype not in ("html_block", "html_inline"):
            continue
        for m in _TAG_NAME_RE.finditer(getattr(tok, "content", "")):
            if m.group(1).lower() in _SAMPLE_KNOWN_TAGS:
                return True
    return False


_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]*)")


def violates_link_scheme(md: str) -> bool:
    """True if a markdown link targets ``file:`` or any scheme other than
    plain http(s)/mailto/relative."""
    for m in _LINK_RE.finditer(md):
        href = m.group(1)
        scheme = href.split(":", 1)[0].lower() if ":" in href.split("/", 1)[0] else ""
        if scheme and scheme not in ("http", "https", "mailto"):
            return True
    return False


def violates_bare_link(md: str) -> bool:
    return bool(re.search(r"\]\(\s*\)", md) or re.search(r"\]\(/\)", md))


# An INDEPENDENT markdown-it instance for the general post-condition below —
# NOT wiki_cleanup._MD, deliberately (this is an oracle, not the module
# under test) — with the same validateLink override applied for the same
# reason wiki_cleanup.py documents: markdown-it's default validator rejects
# file: destinations outright, which would make this checker blind to
# exactly the links/images/autolinks it exists to catch.
_ORACLE_MD = MarkdownIt("commonmark")
_ORACLE_MD.validateLink = lambda url: True  # type: ignore[method-assign]


def assert_no_file_scheme_remains(text: str) -> None:
    """General post-condition used by every fixture test below: no ``file:``
    scheme destination survives ANYWHERE outside code — not in a link, an
    image, a reference definition, or an autolink. Independent of
    wiki_cleanup's own ``_find_link_spans``: walks markdown-it-py's real
    token tree (which already never looks inside code fences/spans for
    link/image/autolink syntax) and each reference definition in ``env``."""
    env: dict[str, object] = {}
    tokens = _ORACLE_MD.parse(text, env)

    references = env.get("references")
    if isinstance(references, dict):
        for ref in references.values():
            if not isinstance(ref, dict):
                continue
            href = str(ref.get("href", ""))
            assert urlsplit(href).scheme.lower() != "file", (
                f"file: reference definition remains: {href!r}"
            )

    def walk(toks: object) -> None:
        for t in toks or []:  # type: ignore[attr-defined]
            if t.type in ("link_open", "image"):
                attrs = dict(t.attrs) if t.attrs else {}
                href = str(attrs.get("href") or attrs.get("src") or "")
                assert urlsplit(href).scheme.lower() != "file", (
                    f"file: {t.type} destination remains: {href!r}"
                )
            walk(getattr(t, "children", None))

    walk(tokens)


def violates_corruption_artifact(md: str) -> bool:
    for rx, _label, _blocking in _ARTIFACT_RES:
        if rx.pattern.startswith(r"<span") or rx.pattern.startswith(r"<div"):
            if rx.search(md):
                return True
    return False


def violates_crlf(md: str) -> bool:
    return "\r" in md


def violates_nbsp(md: str) -> bool:
    return " " in md


def violates_zero_width(md: str) -> bool:
    return bool(re.search("[​‌‍‎‏⁠﻿‪-‮]", md))


def violates_trailing_ws_outside_fence(md: str) -> bool:
    in_fence = False
    for line in re.split(r"\r\n|\n", md):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line != line.rstrip(" \t"):
            return True
    return False


def violates_blank_run(md: str) -> bool:
    return bool(re.search(r"(?:\r?\n){4,}", md))


def violates_final_newline(md: str) -> bool:
    if md == "":
        return False
    return not md.endswith("\n") or md.endswith("\n\n")


def violates_html_table(md: str) -> bool:
    return "<table" in md.lower()


def _top_level_block_type_sequence(md: str) -> list[str]:
    """The document-order sequence of top-level (level 0) block-construct
    token types — paragraph/heading/list/blockquote/hr/fence/code_block/
    html_block. Used to prove an edit didn't silently turn one block type
    into another (e.g. a paragraph into an ordered list) it never meant to."""
    seq = []
    for t in _MD.parse(md):
        if getattr(t, "level", None) != 0:
            continue
        ttype = getattr(t, "type", "")
        if ttype.endswith("_open") or ttype in ("hr", "fence", "code_block", "html_block"):
            seq.append(ttype)
    return seq


# The task's "explicit, tiny list of allowed [visible-text] differences":
# (1) a backslash-escaped punctuation character resolving to its literal form
#     — this module's own visible_text() already resolves these on BOTH
#     sides (see _MD_ESCAPE_RE in wiki_cleanup.py), so a NEW escape this
#     module introduces to guard a block type, e.g. ``9\. w``, already
#     compares equal to the original ``9. w`` with no help needed here;
# (2) a list/heading marker character that was INERT literal text while
#     trapped inside raw HTML (no markdown processing happens there) but
#     becomes ACTIVE markdown syntax once a cross-block corruption-artifact
#     repair (_strip_cross_region_wrappers — identified by an R_ARTIFACT/
#     R_ARTIFACT_HEADING/R_HTML_INLINE Change whose "before" text is
#     literally the opener/closer TAG line itself, e.g.
#     ``<div class="notice-block...`` or ``</div></div>### ...``) exposes
#     it. Scoped two ways, both required, so this can never hide anything
#     else: (a) it activates ONLY when such a Change is actually present —
#     for a page with no div/span cross-block repair at all (every NBSP/
#     marker-drift fixture) it is a complete no-op, full strict comparison;
#     (b) even then, it only touches the exact substring that DIFFERS
#     between before/after (the longest common prefix/suffix of the two
#     visible-text strings is left alone) — never a blanket strip.
_ALLOWED_DIFF_MARKER_RE = re.compile(r"(?:^|(?<=\s))(?:[-*+]|#{1,6})(?=\s|$)")
_CROSS_REGION_WRAPPER_TAG_START = ("<div", "</div", "<span", "</span")


def _has_cross_region_wrapper_change(changes: list[Change]) -> bool:
    return any(
        c.rule in (R_ARTIFACT, R_ARTIFACT_HEADING, R_HTML_INLINE)
        and c.before.lstrip().startswith(_CROSS_REGION_WRAPPER_TAG_START)
        for c in changes
    )


def _visible_text_modulo_allowed_diffs(
    before_md: str, after_md: str, changes: list[Change]
) -> tuple[str, str]:
    vb, va = visible_text(before_md), visible_text(after_md)
    if not _has_cross_region_wrapper_change(changes):
        return re.sub(r"\s+", " ", vb).strip(), re.sub(r"\s+", " ", va).strip()

    i = 0
    while i < len(vb) and i < len(va) and vb[i] == va[i]:
        i += 1
    j = 0
    while j < len(vb) - i and j < len(va) - i and vb[len(vb) - 1 - j] == va[len(va) - 1 - j]:
        j += 1
    vb_mid = _ALLOWED_DIFF_MARKER_RE.sub("", vb[i : len(vb) - j] if j else vb[i:])
    va_mid = _ALLOWED_DIFF_MARKER_RE.sub("", va[i : len(va) - j] if j else va[i:])
    vb2 = vb[:i] + vb_mid + (vb[len(vb) - j :] if j else "")
    va2 = va[:i] + va_mid + (va[len(va) - j :] if j else "")
    return re.sub(r"\s+", " ", vb2).strip(), re.sub(r"\s+", " ", va2).strip()


# --- fixtures ----------------------------------------------------------------


def _read(name: str) -> str:
    # newline="" preserves CRLF verbatim — Path.read_text's universal-newline
    # translation would silently eat the CRLF fixture is meant to exercise.
    with open(FIXTURES / name, encoding="utf-8", newline="") as f:
        return f.read()


ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.md"))
TITLES = {
    "word-export-table.md": "Host Inventory",
    "gallery-images.md": "Network Diagram",
    "placeholder-cheatsheet.md": "SSH Access Commands",
    "hygiene-crlf-nbsp-blank.md": "Hygiene Sample",
    "file-and-bare-links.md": "Old Notes Index",
    "lab-page-22-html-table.md": "Legacy HTML Table Page",
    "lab-page-27-file-links.md": "Old File Links Page",
    "cross-block-notice-heading.md": "Engagement Notes",
    "cross-block-notice-paragraph.md": "Historical Findings",
    "cross-block-notice-eof.md": "Legacy Wiki Import",
    "toc-file-links-block-drift.md": "Table of Contents",
    "blockquote-nbsp-marker-drift.md": "Quoted Reference",
    "list-item-marker-drift-variants.md": "Marker Drift Variants",
    "table-heading-footnote-with-earlier-notice.md": "Assessment Report",
    "footnote-double-bracket-file-link.md": "Footnote Citations",
    "file-link-angle-bracket-and-title.md": "Legacy Link Shapes",
    "file-scheme-image.md": "Embedded Diagram",
    "file-reference-style-links.md": "Reference Style Citations",
    "file-scheme-autolink.md": "Legacy Autolink",
    "file-link-mixed-with-unsupported-scheme.md": "Mixed Scheme Paragraph",
    "footnote-bold-wrapped-file-link.md": "Bold Footnote Citations",
}
OWN_HOSTS = ["192.168.122.177:8443"]


def _clean(name: str) -> tuple[str, CleanResult]:
    md = _read(name)
    result = clean_page(md, title=TITLES.get(name), own_hosts=OWN_HOSTS)
    return md, result


# --- never raises, always idempotent, over every fixture --------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_clean_page_never_raises_on_fixture(name: str) -> None:
    clean_page(_read(name))  # must not raise


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_idempotent_on_fixture(name: str) -> None:
    md, r1 = _clean(name)
    r2 = clean_page(r1.text, title=TITLES.get(name), own_hosts=OWN_HOSTS)
    assert r2.changes == []


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_crash_and_returns_result_for_every_fixture(name: str) -> None:
    _md_text, r = _clean(name)
    assert isinstance(r.text, str)
    assert isinstance(r.changes, list)
    assert isinstance(r.unresolved, list)
    assert isinstance(r.images, list)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_file_scheme_destination_remains_in_any_fixture(name: str) -> None:
    """The general post-condition, over every fixture: after cleanup, no
    link, image, reference definition, or autolink anywhere in the output
    still targets a file: URL — except the ONE case this module deliberately
    leaves untouched (a file: autolink, which has no text to keep and is
    always reported under unresolved instead)."""
    _md_text, r = _clean(name)
    if any("autolink" in i.explanation for i in r.unresolved):
        return  # the one documented exception — proven separately
    assert_no_file_scheme_remains(r.text)


# --- visible-text preservation -----------------------------------------------
#
# Strict for EVERY fixture — reader-visible text must survive every
# transformation exactly, modulo the tiny, explicit, documented list of
# allowed differences in ``_visible_text_modulo_allowed_diffs`` above (a
# title-H1 line is also subtracted first, since removing it is the point of
# that one transformation).


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_visible_text_preserved(name: str) -> None:
    md, r = _clean(name)
    before = md
    title = TITLES.get(name)
    if title:
        before = re.sub(rf"^#\s*{re.escape(title)}\s*\n", "", before, count=1)
    vb, va = _visible_text_modulo_allowed_diffs(before, r.text, r.changes)
    assert vb == va


def test_gallery_image_alt_becomes_caption_visible_text() -> None:
    """An ``<img alt="Legend">`` HTML tag has no reader-visible text of its
    own in a browser (alt only shows if the image fails to load), but this
    module's OWN visible_text() extracts an <img>'s alt attribute as text —
    matching how it already treats a markdown ``![alt](url)``'s alt as
    visible text — precisely so this conversion doesn't look like a loss in
    the strict comparison above."""
    md, r = _clean("gallery-images.md")
    assert "<img" not in r.text
    assert "Legend" in visible_text(r.text)


# --- render_review ------------------------------------------------------------


def test_render_review_lists_changes_and_unresolved() -> None:
    _md_text, r = _clean("word-export-table.md")
    report = render_review(r.changes, r.unresolved)
    assert "Changes" in report
    assert R_HTML_TABLE in report
    for c in r.changes:
        assert str(c.line) in report


def test_render_review_handles_nothing_to_report() -> None:
    report = render_review([], [])
    assert "none" in report.lower()


def test_render_review_lists_unresolved_with_line_numbers() -> None:
    _md_text, r = _clean("colspan-table-unresolved.md")
    report = render_review(r.changes, r.unresolved)
    assert "human" in report.lower()
    assert str(r.unresolved[0].line) in report


def test_render_review_lists_images_to_fetch() -> None:
    """own_hosts matching is what puts a URL in CleanResult.images; the
    review report surfaces it so the reviewer sees, before anything is
    pushed, which absolute gallery URLs will be downloaded into images/."""
    _md_text, r = _clean("gallery-images.md")
    report = render_review(r.changes, r.unresolved, r.images)
    assert "images/" in report
    for img in r.images:
        assert img.url in report
        assert f"images/{img.proposed_filename}" in report


def test_render_review_images_default_is_empty() -> None:
    report = render_review([], [])
    assert "Images to fetch into images/: none" in report


# =============================================================================
# Proofs: BEFORE violates, AFTER does not — one per transformation.
# =============================================================================


def test_html_table_converted_to_markdown_table() -> None:
    md, r = _clean("word-export-table.md")
    assert violates_html_table(md)
    assert not violates_html_table(r.text)
    assert violates_raw_html(md)
    assert not violates_raw_html(r.text)
    assert any(c.rule == R_HTML_TABLE for c in r.changes)
    assert "| web01 |" in r.text or "web01" in r.text
    assert "| Host | Role | Notes |" in r.text


def test_headerless_table_infers_header_row() -> None:
    md, r = _clean("headerless-table.md")
    assert violates_html_table(md)
    assert not violates_html_table(r.text)
    assert "header-row-inferred" in {c.rule for c in r.changes}
    assert "| Severity | SLA |" in r.text


def test_colspan_table_left_unresolved() -> None:
    md, r = _clean("colspan-table-unresolved.md")
    assert violates_html_table(md)
    assert violates_html_table(r.text)  # left untouched — never guessed at
    assert r.text == md
    assert len(r.unresolved) == 1
    assert "colspan" in r.unresolved[0].explanation or "unsupported" in r.unresolved[0].explanation


def test_nested_table_left_unresolved() -> None:
    md, r = _clean("nested-table-unresolved.md")
    assert r.text == md
    assert len(r.unresolved) == 1


def test_unknown_html_tag_left_unresolved() -> None:
    md, r = _clean("unknown-tag-unresolved.md")
    assert violates_raw_html(md)
    assert violates_raw_html(r.text)  # <iframe> left as-is, never guessed at
    assert r.text == md
    assert len(r.unresolved) == 1


def test_span_and_div_wrappers_unwrapped() -> None:
    md, r = _clean("word-export-table.md")
    assert "<span" in md and "<p class" in md
    assert "<span" not in r.text
    assert "<p class" not in r.text
    assert any(c.rule == R_HTML_INLINE for c in r.changes)


def test_a_tag_and_strong_converted_inline() -> None:
    md, r = _clean("word-export-table.md")
    assert violates_raw_html(md)
    assert not violates_raw_html(r.text)
    assert "**public**" in r.text
    assert "[runbook](https://internal.example.com/db)" in r.text


def test_file_link_stripped_keeps_text_drops_target() -> None:
    md, r = _clean("file-and-bare-links.md")
    assert violates_link_scheme(md)
    assert "file:" not in r.text
    assert "Local note" in r.text
    assert any(c.rule == R_FILE_LINK for c in r.changes)


def test_lab_page_27_all_twelve_file_links_stripped() -> None:
    md, r = _clean("lab-page-27-file-links.md")
    assert md.count("file:///") == 12
    assert "file:" not in r.text
    assert sum(1 for c in r.changes if c.rule == R_FILE_LINK) == 12
    for i in range(1, 13):
        assert f"Old note {i}" in r.text


def test_bare_and_empty_links_unwrapped_to_text() -> None:
    md, r = _clean("file-and-bare-links.md")
    assert violates_bare_link(md)
    assert not violates_bare_link(r.text)
    assert any(c.rule == R_BARE_LINK for c in r.changes)
    assert "Empty target" in r.text and "Slash target" in r.text


def test_ftp_link_scheme_left_unresolved() -> None:
    md, r = _clean("file-and-bare-links.md")
    assert "ftp://" in r.text  # never guessed at
    assert any("ftp" in i.explanation or "scheme" in i.explanation for i in r.unresolved)


def test_real_http_link_untouched() -> None:
    md, r = _clean("file-and-bare-links.md")
    assert "[Real link](https://example.com/docs)" in r.text


# =============================================================================
# Proofs: file: links regardless of bracket nesting or markdown-it's own
# link-validation rejection (locating via a permissive-validateLink
# markdown-it instance — see wiki_cleanup._MD's own docstring).
# =============================================================================


def test_double_bracket_footnote_file_link_escaped_brackets() -> None:
    """The reported real-page shape: a footnote citation ``[[9]](file:///...)``
    — CommonMark reads the double brackets as a link whose TEXT is literally
    ``[9]``. markdown-it's DEFAULT validateLink rejects file: outright, so
    without the permissive override this construct is invisible (stays
    literal text, never even recognized as a link) — proof both that it's
    found AND that the kept text's own brackets are escaped so they can
    never pair with whatever follows and form a NEW link."""
    md, r = _clean("footnote-double-bracket-file-link.md")
    assert_no_file_scheme_remains(r.text)
    assert "\\[9\\]" in r.text
    assert "\\[12\\]" in r.text
    assert "[[9]]" not in r.text and "[[12]]" not in r.text
    assert sum(1 for c in r.changes if c.rule == R_FILE_LINK) == 2
    # the escaped brackets can never accidentally pair with a later "(...)"
    assert not re.search(r"\[9\]\([^)]*\)", r.text)


def test_angle_bracket_destination_and_titled_file_link_both_stripped() -> None:
    md, r = _clean("file-link-angle-bracket-and-title.md")
    assert_no_file_scheme_remains(r.text)
    assert "the note for the raw file with spaces" in r.text
    assert "the other note for a link with a title" in r.text
    assert "<file:" not in r.text
    assert '"Old Note Title"' not in r.text
    assert sum(1 for c in r.changes if c.rule == R_FILE_LINK) == 2


def test_file_scheme_image_keeps_only_alt_text() -> None:
    md, r = _clean("file-scheme-image.md")
    assert_no_file_scheme_remains(r.text)
    assert "![" not in r.text
    assert "Network topology diagram" in r.text
    assert any(c.rule == R_FILE_LINK for c in r.changes)


def test_file_reference_style_and_shortcut_links_stripped_definitions_removed() -> None:
    md, r = _clean("file-reference-style-links.md")
    assert_no_file_scheme_remains(r.text)
    assert "[ref1]:" not in r.text and "[ref2]:" not in r.text
    assert "old assessment note" in r.text
    assert "ref2" in r.text  # the shortcut usage's own text survives
    assert "[old assessment note][ref1]" not in r.text
    assert "[ref2]" not in r.text or "[ref2]:" not in r.text
    assert sum(1 for c in r.changes if c.rule == R_FILE_LINK) == 4  # 2 usages + 2 definitions


def test_file_scheme_autolink_left_unresolved_no_text_to_keep() -> None:
    """An autolink ``<file:///...>`` has no separate text — CommonMark
    renders the URL itself as the visible text — so stripping the target
    would leave nothing behind; this module never guesses, so it's left
    completely untouched and reported."""
    md, r = _clean("file-scheme-autolink.md")
    assert "<file:///C:/notes/note1.txt>" in r.text
    assert any("autolink" in i.explanation and "file:" in i.explanation for i in r.unresolved)
    assert not any(c.rule == R_FILE_LINK for c in r.changes)


def test_no_file_scheme_general_postcondition_helper_catches_a_real_violation() -> None:
    """Proves assert_no_file_scheme_remains actually detects what it claims
    to — used as a blanket post-condition over every fixture above, so this
    confirms it isn't vacuously passing."""
    with pytest.raises(AssertionError):
        assert_no_file_scheme_remains("[text](file:///C:/still/here.txt)\n")
    with pytest.raises(AssertionError):
        assert_no_file_scheme_remains("![alt](file:///C:/still/here.png)\n")
    with pytest.raises(AssertionError):
        assert_no_file_scheme_remains("[ref]\n\n[ref]: file:///C:/still/here.txt\n")
    with pytest.raises(AssertionError):
        assert_no_file_scheme_remains("<file:///C:/still/here.txt>\n")
    assert_no_file_scheme_remains("plain text, no links at all\n")  # no violation: passes


def test_corruption_artifacts_well_formed_repaired() -> None:
    md, r = _clean("corruption-artifacts.md")
    assert violates_corruption_artifact(md)
    rules = {c.rule for c in r.changes}
    assert R_ARTIFACT in rules
    assert 'class="citation-1"' not in r.text
    assert 'class="notice' not in r.text
    assert "[1]" in r.text
    assert "Scope was limited to the DMZ" in r.text


def test_corruption_artifact_malformed_left_unresolved() -> None:
    md, r = _clean("corruption-artifacts.md")
    # the well-formed instances are gone, but the deliberately unclosed one
    # must still be present (never guessed at) and reported
    assert 'class="citation-2"' in r.text
    assert any("malformed" in i.explanation for i in r.unresolved)


def test_cross_block_notice_div_restores_heading() -> None:
    """A notice-block div opened in one html_block, closed in a LATER one
    (severed by a blank line) after real content — proof that pairing
    happens at DOCUMENT level, not per-block, and that the closer glued to
    what was always meant to be a heading gets promoted, explicitly labeled."""
    md, r = _clean("cross-block-notice-heading.md")
    assert "<div" not in r.text and "</div>" not in r.text
    assert "- Internal network only" in r.text
    assert "### Executive Summary (Scope)" in r.text
    rules = [c.rule for c in r.changes]
    assert rules.count(R_ARTIFACT) == 1  # the opener
    assert rules.count(R_ARTIFACT_HEADING) == 1  # the closer, explicitly labeled
    assert r.unresolved == []


def test_cross_block_notice_div_becomes_plain_paragraph() -> None:
    """Variant 2: the closer is glued to the start of an ordinary paragraph,
    not a heading — no promotion, just the ordinary artifact-repaired label
    on both sides."""
    md, r = _clean("cross-block-notice-paragraph.md")
    assert "<div" not in r.text and "</div>" not in r.text
    assert "- Findings below reflect state as of the assessment date" in r.text
    assert r.text.count("Results should be independently verified before relying on them") == 1
    assert not r.text.split("\n\n")[3].startswith("#")
    rules = [c.rule for c in r.changes]
    assert rules.count(R_ARTIFACT) == 2
    assert R_ARTIFACT_HEADING not in rules
    assert r.unresolved == []


def test_cross_block_notice_div_closer_alone_at_eof() -> None:
    """Variant 3: the closer is the LAST line of the page with nothing after
    it — it just disappears (and the trailing blank line it leaves behind is
    cleaned up by the ordinary final-newline hygiene rule)."""
    md, r = _clean("cross-block-notice-eof.md")
    assert "<div" not in r.text and "</div>" not in r.text
    assert r.text.endswith("Only two remain in production today\n")
    assert not r.text.endswith("\n\n")
    rules = [c.rule for c in r.changes]
    assert rules.count(R_ARTIFACT) == 2
    assert r.unresolved == []


def test_cross_block_pairing_is_document_level_not_per_block() -> None:
    """The bug this fixes directly: judging each html_block in isolation
    sees an unclosed <div><div> in the opener block and two orphaned </div>
    in the closer block, both "malformed" — proof that BEFORE this module's
    own document-level view, the naive per-block oracle would call both
    blocks unresolved, while clean_page resolves them."""
    # The opener block, judged alone, has 2 unclosed <div>s:
    opener_block = (
        '<div class="notice-block info" id="bkmrk-scope-notes"><div class="content">'
        "- Internal network only\n- No physical access\n- No social engineering\n"
        "- Business hours only\n"
    )
    assert violates_raw_html(opener_block)
    closer_block = "</div></div>### Executive Summary (Scope)\n"
    assert violates_raw_html(closer_block)
    _md_text, r = _clean("cross-block-notice-heading.md")
    assert r.unresolved == []  # clean_page resolves both, judged together


def test_toc_file_link_unwrap_preserves_paragraph_not_list() -> None:
    """The reported real-world bug: a standalone ``[9. Appendix A](file:///...)``
    paragraph unwraps to ``9. Appendix A`` — which CommonMark reads as an
    ORDERED LIST start unless escaped. Proof: the file link is gone, AND the
    block-type sequence (paragraph, not list) is unchanged."""
    md, r = _clean("toc-file-links-block-drift.md")
    assert "file:" not in r.text
    assert "9\\. Appendix A - Raw Scan Output" in r.text
    before_body = "\n".join(md.split("\n")[2:])  # drop the title H1 + blank line
    before_types = _top_level_block_type_sequence(before_body)
    after_types = _top_level_block_type_sequence(r.text)
    assert before_types == after_types == ["paragraph_open"] * 3
    assert sum(1 for c in r.changes if c.rule == R_FILE_LINK) == 3


def test_toc_link_text_survives_escape_in_visible_text() -> None:
    md, r = _clean("toc-file-links-block-drift.md")
    assert "9. Appendix A - Raw Scan Output" in visible_text(r.text)


def test_block_type_drift_escape_is_idempotent_and_renders_same_digit() -> None:
    r1 = clean_page("[9. Item](file:///x.htm)\n")
    assert r1.text == "9\\. Item\n"
    r2 = clean_page(r1.text)
    assert r2.changes == []


def test_bullet_marker_drift_escaped_too() -> None:
    """Same block-drift guard, bullet-list shape: a standalone link whose
    text starts with "- " reads as a bullet list once unwrapped, unless
    escaped."""
    r = clean_page("[- classified note](file:///x.htm)\n")
    assert r.text == "\\- classified note\n"
    assert _top_level_block_type_sequence(r.text) == ["paragraph_open"]


def test_heading_marker_drift_escaped_too() -> None:
    r = clean_page("[# Not A Heading](file:///x.htm)\n")
    assert r.text == "\\# Not A Heading\n"
    assert _top_level_block_type_sequence(r.text) == ["paragraph_open"]


def _full_block_type_level_sequence(md: str) -> list[tuple[str, int]]:
    """Like ``_top_level_block_type_sequence`` but at EVERY nesting level —
    proves a list item's or blockquote's OWN content stayed a plain
    paragraph and didn't grow an unwanted NESTED list/heading/blockquote."""
    seq = []
    for t in _MD.parse(md):
        ttype = getattr(t, "type", "")
        if ttype.endswith("_open") or ttype in ("hr", "fence", "code_block", "html_block"):
            seq.append((ttype, getattr(t, "level", -1)))
    return seq


def test_nbsp_padded_list_item_marker_drift_the_reported_257kb_page_bug() -> None:
    """The exact reported shape: a bullet list whose items are standalone
    ``[9.\\xa0\\xa0\\xa0\\xa0 text](file:///...#_Toc9)`` links. Judged BEFORE
    NBSP normalization, "9.\\xa0\\xa0" isn't marker whitespace so there's no
    drift to see yet — the drift is a PRODUCT of hygiene, not of the link
    unwrap, which is exactly why the fast per-edit guard misses it and the
    final, after-all-passes guard is required. ``9.9...`` (not a valid
    ordered marker even after NBSP->space) is proof the fix isn't
    over-eager: it's untouched, because it was never actually a list start."""
    md, r = _clean("toc-list-item-nbsp-marker-drift.md")
    assert "file:" not in r.text
    assert " " not in r.text  # NBSP normalized
    assert r.unresolved == []
    lines = r.text.splitlines()
    assert lines[2] == "- Executive Summary"
    assert lines[3] == "- 9\\.     Appendix A - Raw Scan Output"
    assert lines[4] == "- 9.9    Appendix B - Detailed Findings"  # never a marker — untouched
    assert lines[5] == "- 9\\.     Recommendations.. Next Steps"
    assert sum(1 for c in r.changes if c.rule == R_BLOCK_DRIFT) == 2
    # every item stayed a single bullet_list > list_item > paragraph — no
    # accidental nested list appeared anywhere
    seq = _full_block_type_level_sequence(r.text)
    kinds = [t for t, _lvl in seq]
    assert kinds.count("bullet_list_open") == 1
    assert kinds.count("ordered_list_open") == 0
    assert kinds.count("list_item_open") == 4
    r2 = clean_page(r.text)
    assert r2.changes == []


def test_nbsp_padded_marker_drift_blockquote_analogue() -> None:
    md, r = _clean("blockquote-nbsp-marker-drift.md")
    assert "file:" not in r.text
    assert " " not in r.text
    assert r.unresolved == []
    assert "> 9\\.     Appendix A - Raw Scan Output" in r.text
    seq = _full_block_type_level_sequence(r.text)
    kinds = [t for t, _lvl in seq]
    assert kinds.count("blockquote_open") == 1
    assert kinds.count("ordered_list_open") == 0
    assert any(c.rule == R_BLOCK_DRIFT for c in r.changes)
    r2 = clean_page(r.text, title="Quoted Reference")
    assert r2.changes == []


def test_marker_drift_variants_1_paren_and_hash_inside_list_items() -> None:
    """The ``1)``-style ordered marker and ``#``-style heading marker, both
    as a list item's own content, plus a depth-3 nested item proving the
    guard works at any nesting depth, not just depth 1."""
    md, r = _clean("list-item-marker-drift-variants.md")
    assert "file:" not in r.text
    assert " " not in r.text
    assert r.unresolved == []
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert lines[0] == "- 1\\)    First deliverable"
    assert lines[1] == "- \\# Not actually a heading"
    assert lines[2] == "  - nested item stays a bullet"
    assert lines[3] == "    - 9\\.     nested ordered-looking text"
    seq = _full_block_type_level_sequence(r.text)
    kinds = [t for t, _lvl in seq]
    assert kinds.count("ordered_list_open") == 0
    assert kinds.count("heading_open") == 0
    assert kinds.count("bullet_list_open") == 3  # top level + 2 nested levels
    assert sum(1 for c in r.changes if c.rule == R_BLOCK_DRIFT) == 2
    r2 = clean_page(r.text, title="Marker Drift Variants")
    assert r2.changes == []


def test_old_blanket_marker_allowance_would_have_hidden_the_hash_drift_bug() -> None:
    """Why the visible-text allowance was narrowed, proven concretely (not
    just asserted): take the "#"-style marker-drift shape from
    list-item-marker-drift-variants.md — a list item whose content is
    literally "# Not actually a heading" — in both its CORRECTLY escaped
    form and the UNFIXED-drift form a bug would have produced (the "#" left
    unescaped, so CommonMark reads it as a real nested heading and the "#"
    itself is consumed as syntax, never reaching the text layer).

    A strict comparison correctly tells these apart (the escaped form's "#"
    resolves to a literal, visible "#" character; the drifted form's doesn't,
    since a real heading marker is never part of its own visible text). But
    the OLD, unconditional, whole-text "strip every bare -/#" allowance
    strips the literal "#" off the CORRECT form too — making it compare
    EQUAL to the buggy one. That's the exact failure mode this test proves
    against the real fixture text, standalone, without needing clean_page to
    currently be broken (it isn't)."""
    fixed = "- \\# Not actually a heading\n"  # what clean_page actually produces
    unfixed_drift = "- # Not actually a heading\n"  # what an unescaped drift bug would produce

    vb_fixed = visible_text(fixed)
    vb_drift = visible_text(unfixed_drift)
    assert vb_fixed != vb_drift  # strict comparison: correctly tells them apart

    old_blanket_re = re.compile(r"(?:^|(?<=\s))(?:[-*+]|#{1,6})(?=\s)")

    def old_style_normalize(text: str) -> str:
        return re.sub(r"\s+", " ", old_blanket_re.sub("", text)).strip()

    assert old_style_normalize(vb_fixed) == old_style_normalize(vb_drift)  # masked!

    # The actual fixture, through the real (already-fixed) clean_page,
    # produces no cross-region-wrapper Change at all, so the CURRENT,
    # narrowed allowance applies ZERO normalization here — full strict
    # comparison, the failure mode above cannot occur for this fixture.
    md, r = _clean("list-item-marker-drift-variants.md")
    before = re.sub(r"^#\s*Marker Drift Variants\s*\n", "", md, count=1)
    assert not _has_cross_region_wrapper_change(r.changes)
    vb, va = _visible_text_modulo_allowed_diffs(before, r.text, r.changes)
    strict_vb = re.sub(r"\s+", " ", visible_text(before)).strip()
    strict_va = re.sub(r"\s+", " ", visible_text(r.text)).strip()
    assert (vb, va) == (strict_vb, strict_va)  # confirmed: no normalization applied


_PLACEHOLDER_TAG_RE = re.compile(r"<(?:user|password|domain|target|ip|hash|jumphost|bastion)>")


def test_block_type_sequence_preserved_property_on_fixtures_without_raw_html() -> None:
    """For every fixture that never contained raw HTML to begin with (so
    nothing was ever a "deliberately converted" block), the top-level
    block-type sequence must be byte-for-byte the same before and after —
    except a fixture whose OWN unresolved report says a line's structure
    genuinely could not be repaired (the one documented, intentional
    exception: an unrepairable dead end is reported and left exactly as
    hygiene produced it, not silently forced back to matching, see
    ``toc-drift-with-unrelated-dead-end.md`` and ``_verify_block_structure``)."""
    html_free = [n for n in ALL_FIXTURES if "<" not in _PLACEHOLDER_TAG_RE.sub("", _read(n))]
    assert html_free  # sanity: at least one fixture actually qualifies
    for name in html_free:
        md, r = _clean(name)
        if any("could not be safely repaired" in i.explanation for i in r.unresolved):
            continue
        before = md
        title = TITLES.get(name)
        if title:
            before = re.sub(rf"^#\s*{re.escape(title)}\s*\n", "", before, count=1)
            after = re.sub(rf"^#\s*{re.escape(title)}\s*\n", "", r.text, count=1)
        else:
            after = r.text
        assert _top_level_block_type_sequence(before) == _top_level_block_type_sequence(after)


def test_gallery_image_reported_without_mapping() -> None:
    md, r = _clean("gallery-images.md")
    assert len(r.images) == 2
    urls = {i.url for i in r.images}
    assert any(u.endswith("network-diagram.png") for u in urls)
    assert any(u.endswith("legend.png") for u in urls)
    filenames = {i.proposed_filename for i in r.images}
    assert filenames == {"network-diagram.png", "legend.png"}


def test_gallery_image_rewritten_with_mapping() -> None:
    md = _read("gallery-images.md")
    mapping = {
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/network-diagram.png": (
            "network-diagram.png"
        ),
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/legend.png": "legend.png",
    }
    r = clean_page(md, title="Network Diagram", own_hosts=OWN_HOSTS, image_map=mapping)
    assert "![Diagram](images/network-diagram.png)" in r.text
    assert "![Legend](images/legend.png)" in r.text
    assert "192.168.122.177" not in r.text
    assert r.images == []
    assert any(c.rule == R_GALLERY_IMAGE for c in r.changes)
    # a second pass with the same mapping changes nothing further
    r2 = clean_page(r.text, own_hosts=OWN_HOSTS, image_map=mapping)
    assert r2.changes == []


def test_external_image_left_alone() -> None:
    md, r = _clean("gallery-images.md")
    assert "![External](https://cdn.example.com/logo.png)" in r.text
    assert not any(i.url.endswith("logo.png") for i in r.images)


# --- item 4a: host-relative gallery URL (no scheme/host at all) --------------


def test_host_relative_gallery_reference_is_recognized_given_bs_host() -> None:
    """Before the fix: clean_page only recognized a FULLY-QUALIFIED
    ``https://<host>/uploads/images/...`` gallery reference — a real
    BookStack export also stores the host-relative spelling,
    ``/uploads/images/...`` (no scheme/host), which was left completely
    unrecognized (never reported, never rewritten) even though it names the
    exact same upload."""
    md = "![Diagram](/uploads/images/gallery/2026-09/network-diagram.png)\n"
    r = clean_page(md, bs_host="192.168.122.177:8443")
    assert len(r.images) == 1
    assert r.images[0].url == (
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/network-diagram.png"
    )
    assert r.images[0].proposed_filename == "network-diagram.png"


def test_host_relative_gallery_reference_unrecognized_without_bs_host() -> None:
    """No ``bs_host`` given: nothing to build a canonical absolute URL from,
    so a host-relative reference is left exactly as before this fix —
    unrecognized, not guessed at."""
    md = "![Diagram](/uploads/images/gallery/2026-09/network-diagram.png)\n"
    r = clean_page(md)
    assert r.images == []
    assert "/uploads/images/gallery/2026-09/network-diagram.png" in r.text


def test_host_relative_and_absolute_spellings_of_the_same_image_share_one_url() -> None:
    """A page mixing both spellings for the SAME upload (both occur in real
    pages) must be recognized as the SAME file, not two — both resolve to
    the identical canonical absolute URL."""
    md = (
        "![A](https://192.168.122.177:8443/uploads/images/gallery/2026-09/diagram.png)\n\n"
        "![B](/uploads/images/gallery/2026-09/diagram.png)\n"
    )
    r = clean_page(md, own_hosts=OWN_HOSTS, bs_host="192.168.122.177:8443")
    assert {i.url for i in r.images} == {
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/diagram.png"
    }
    assert {i.proposed_filename for i in r.images} == {"diagram.png"}


def test_host_relative_gallery_reference_rewritten_with_mapping() -> None:
    md = "![Diagram](/uploads/images/gallery/2026-09/network-diagram.png)\n"
    mapping = {
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/network-diagram.png": (
            "network-diagram.png"
        ),
    }
    r = clean_page(md, bs_host="192.168.122.177:8443", image_map=mapping)
    assert "![Diagram](images/network-diagram.png)" in r.text
    assert "/uploads/images/" not in r.text
    r2 = clean_page(r.text, bs_host="192.168.122.177:8443", image_map=mapping)
    assert r2.changes == []  # idempotent


# --- item 4b: REF-007's two local spellings (book root vs. inside a chapter) --


def test_gallery_image_rewritten_with_the_dotdot_spelling_inside_a_chapter() -> None:
    """Before the fix: clean_page always emitted the book-root ``images/<file>``
    spelling regardless of where the page actually lives — REF-007 requires
    ``../images/<file>`` for a page one level down, inside a chapter."""
    md = _read("gallery-images.md")
    mapping = {
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/network-diagram.png": (
            "network-diagram.png"
        ),
        "https://192.168.122.177:8443/uploads/images/gallery/2026-09/legend.png": "legend.png",
    }
    r = clean_page(
        md,
        title="Network Diagram",
        own_hosts=OWN_HOSTS,
        in_chapter=True,
        image_map=mapping,
    )
    assert "![Diagram](../images/network-diagram.png)" in r.text
    assert "![Legend](../images/legend.png)" in r.text
    assert "images/network-diagram.png)" not in r.text.replace("../images/network-diagram.png)", "")
    r2 = clean_page(
        r.text,
        own_hosts=OWN_HOSTS,
        in_chapter=True,
        image_map=mapping,
    )
    assert r2.changes == []  # idempotent


def test_book_root_page_still_gets_the_bare_images_spelling() -> None:
    """The default (in_chapter=False) is unchanged — a book-root page keeps
    getting the plain images/<file> spelling."""
    md = "![Diagram](https://192.168.122.177:8443/uploads/images/gallery/x/a.png)\n"
    mapping = {"https://192.168.122.177:8443/uploads/images/gallery/x/a.png": "a.png"}
    r = clean_page(md, own_hosts=OWN_HOSTS, image_map=mapping)
    assert "![Diagram](images/a.png)" in r.text


# --- idempotence on the masked real-shape page (read-only, never copied) -----

# A masked (content-scrubbed, structure-preserved) real page from the rework lab,
# kept outside this repo; point GRISON_REAL_SHAPE_PAGE at it to run this check.
_REAL_SHAPE_PAGE = Path(os.environ.get("GRISON_REAL_SHAPE_PAGE", "/nonexistent"))


@pytest.mark.skipif(not _REAL_SHAPE_PAGE.is_file(), reason="lab real-shape fixture not present")
def test_idempotent_on_the_masked_real_shape_page() -> None:
    """Reads the real, masked (content-scrubbed, structure-preserved) page
    directly from the lab worktree — never copied into this repo — and
    proves clean_page settles: a second pass over its own output makes no
    further changes."""
    md = _REAL_SHAPE_PAGE.read_text(encoding="utf-8")
    r1 = clean_page(md, own_hosts=OWN_HOSTS, bs_host="192.168.122.177:8443")
    r2 = clean_page(r1.text, own_hosts=OWN_HOSTS, bs_host="192.168.122.177:8443")
    assert r2.changes == []


_CHEATSHEET_PLACEHOLDERS = (
    "<user>", "<password>", "<domain>", "<target>", "<ip>", "<hash>", "<jumphost>", "<bastion>",
)  # fmt: skip


def test_placeholders_never_touched_or_reported() -> None:
    md, r = _clean("placeholder-cheatsheet.md")
    body_without_title = md.split("\n", 2)[2]
    assert r.text.endswith(body_without_title.rstrip("\n") + "\n")
    for placeholder in _CHEATSHEET_PLACEHOLDERS:
        assert placeholder in r.text
    assert r.changes == [Change(R_TITLE_H1, 1, r.changes[0].before, "")]
    assert r.unresolved == []


def test_crlf_normalized_to_lf() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert violates_crlf(md)
    assert not violates_crlf(r.text)
    assert any(c.rule == R_CRLF for c in r.changes)


def test_nbsp_normalized_outside_code() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert violates_nbsp(md)
    assert "non-breaking space." not in r.text
    assert any(c.rule == R_NBSP for c in r.changes)


def test_nbsp_kept_inside_code_fence() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert "non-breaking space kept as-is" in r.text  # untouched inside the fence


def test_zero_width_and_bidi_removed_outside_code() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert violates_zero_width(md)
    assert not violates_zero_width(r.text)
    assert any(c.rule == R_CONTROL_CHAR for c in r.changes)


def test_trailing_whitespace_stripped_outside_fence() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert violates_trailing_ws_outside_fence(md)
    assert not violates_trailing_ws_outside_fence(r.text)
    assert any(c.rule == R_TRAILING_WS for c in r.changes)


def test_trailing_whitespace_kept_inside_fence() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert "code line with trailing spaces   \n" in r.text


def test_blank_lines_collapsed_to_two() -> None:
    md, r = _clean("hygiene-crlf-nbsp-blank.md")
    assert violates_blank_run(md)
    assert not violates_blank_run(r.text)
    assert any(c.rule == R_BLANK_LINES for c in r.changes)


def test_final_newline_exactly_one() -> None:
    for text in ("no newline at all", "two\n\n", "three\n\n\n", "", "fine\n"):
        r = clean_page(text)
        assert not violates_final_newline(r.text)


def test_final_newline_change_reported_only_when_needed() -> None:
    r_needs_fix = clean_page("hello")
    assert any(c.rule == R_FINAL_NEWLINE for c in r_needs_fix.changes)
    r_already_fine = clean_page("hello\n")
    assert not any(c.rule == R_FINAL_NEWLINE for c in r_already_fine.changes)


def test_title_h1_removed_when_matches_title() -> None:
    r = clean_page("# My Page\n\nBody text.\n", title="My Page")
    assert "# My Page" not in r.text
    assert "Body text." in r.text
    assert any(c.rule == R_TITLE_H1 for c in r.changes)


def test_title_h1_kept_when_no_title_given() -> None:
    r = clean_page("# My Page\n\nBody text.\n")
    assert "# My Page" in r.text
    assert not any(c.rule == R_TITLE_H1 for c in r.changes)


def test_title_h1_kept_when_it_does_not_match_title() -> None:
    r = clean_page("# Something Else\n\nBody text.\n", title="My Page")
    assert "# Something Else" in r.text


def test_title_h1_only_removed_when_first_block() -> None:
    r = clean_page("Intro paragraph.\n\n# My Page\n\nBody.\n", title="My Page")
    assert "# My Page" in r.text  # not the FIRST block, so left alone


# --- HTML-tag-shaped placeholder trap (the brief's headline gotcha) --------


def test_command_placeholders_not_mistaken_for_html() -> None:
    md = (
        "Run `curl -u <user>:<password> <domain>` then connect to <ip> and use "
        "<hash> against <target>.\n"
    )
    r = clean_page(md)
    assert r.text == md
    assert r.changes == []
    assert r.unresolved == []


def test_placeholder_next_to_real_html_both_handled_correctly() -> None:
    md = 'Value is <domain> wrapped in <span class="x">emphasis</span>.\n'
    r = clean_page(md)
    assert "<domain>" in r.text  # untouched, never reported
    assert "<span" not in r.text  # real HTML, unwrapped
    assert "emphasis" in r.text
    # exactly the span unwrap happened — no rule/issue exists purely about the
    # placeholder (it just rides along inside the same paragraph's excerpt)
    assert {c.rule for c in r.changes} == {R_HTML_INLINE}
    assert r.unresolved == []


# --- code spans / fences are never touched ----------------------------------


def test_html_tag_shaped_text_inside_code_span_untouched() -> None:
    md = "Compare `a < b` and `<span>not real</span>` literally.\n"
    r = clean_page(md)
    assert r.text == md
    assert r.changes == []


def test_html_tag_shaped_text_inside_fence_untouched() -> None:
    md = "```html\n<table><tr><td>literal</td></tr></table>\n```\n"
    r = clean_page(md)
    assert r.text == md
    assert r.changes == []


# --- never raises, arbitrary input ------------------------------------------


@given(st.text(max_size=500))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_never_raises_on_arbitrary_text(text: str) -> None:
    clean_page(text)


@given(st.text(max_size=500), st.text(max_size=40))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_never_raises_with_title_and_hosts(text: str, title: str) -> None:
    clean_page(text, title=title, own_hosts=["example.com"])


# --- hypothesis property over generated "legacy-looking" pages -------------

_PLACEHOLDERS = ["<domain>", "<user>", "<password>", "<ip>", "<hash>", "<target>"]
_PROSE_WORDS = ["scan", "host", "found", "vulnerable", "report", "the", "server", "was"]
_HTML_SNIPPETS = [
    '<span class="hl">x</span>',
    "<div>y</div>",
    "<table><tr><td>a</td><td>b</td></tr></table>",
    "<strong>bold</strong>",
]

_legacy_fragment = st.one_of(
    st.sampled_from(_PROSE_WORDS),
    st.sampled_from(_PLACEHOLDERS),
    st.sampled_from(_HTML_SNIPPETS),
)

_legacy_page = st.lists(_legacy_fragment, min_size=1, max_size=25).map(
    lambda parts: " ".join(parts)
)


@given(_legacy_page)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_legacy_looking_pages_never_raise_and_are_idempotent(page: str) -> None:
    r1 = clean_page(page)
    r2 = clean_page(r1.text)
    assert r2.changes == []


@given(_legacy_page)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_legacy_looking_pages_placeholders_always_survive(page: str) -> None:
    r = clean_page(page)
    for p in _PLACEHOLDERS:
        assert page.count(p) == r.text.count(p)


# --- hypothesis property: block-type drift, over generated TOC-like pages ---
#
# No raw HTML anywhere in this generator's output — every paragraph is a
# standalone ``[marker-shaped text](file:///...)`` link or plain prose, blank-
# line separated, so none of it is a "deliberately converted" block; the
# top-level block-type sequence (always "paragraph_open" here) must survive
# clean_page exactly, per the task's property requirement.
_MARKER_PREFIXES = ["9. ", "1) ", "- ", "* ", "+ ", "# ", "### ", "> ", "--- ", "=== "]
_TOC_WORDS = ["Appendix", "Scope", "Introduction", "Methodology", "Findings", "of", "the"]

_toc_paragraph = st.builds(
    lambda prefix, words: f"[{prefix}{' '.join(words)}](file:///C:/old/report.htm#_Toc1)",
    st.sampled_from(_MARKER_PREFIXES),
    st.lists(st.sampled_from(_TOC_WORDS), min_size=1, max_size=5),
)
_prose_paragraph = st.lists(st.sampled_from(_PROSE_WORDS), min_size=1, max_size=8).map(" ".join)
_toc_page = st.lists(st.one_of(_toc_paragraph, _prose_paragraph), min_size=1, max_size=12).map(
    "\n\n".join
)


@given(_toc_page)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_generated_toc_pages_never_drift_block_type(page: str) -> None:
    before_types = _top_level_block_type_sequence(page)
    r = clean_page(page)
    after_types = _top_level_block_type_sequence(r.text)
    assert before_types == after_types
    assert set(before_types) <= {"paragraph_open"}


# --- hypothesis property: marker-shaped link text + NBSP/space runs INSIDE --
# --- list items and blockquotes at depth 1-2 (the reported 257KB-page bug) --
#
# Generates whole documents built from "container blocks" — a top-level
# bullet list (with each item optionally having ONE depth-2 nested item), a
# blockquote, a blockquote directly containing one list item (blockquote >
# list, also depth 2), or plain prose — where each item/blockquote's own
# content is either ordinary prose or a standalone
# ``[marker-prefix<NBSP-run> words](file:///...)`` link (the marker prefixes
# here exclude "-"/"*"/"+" specifically because THIS generator also creates
# genuine bullet-list containers of its own — using them inside fragments
# too would make "how many bullet_list_open tokens SHOULD exist" ambiguous;
# ordered/heading/blockquote/thematic-break markers, which this generator
# never uses as real containers, are unambiguous). Each block tracks exactly
# how many bullet_list_open / blockquote_open / list_item_open tokens it is
# SUPPOSED to contribute, so the property can assert an EXACT count in the
# output — proving no marker-shaped item content ever silently adds an
# extra (nested) container of any kind, at any of the generated depths.
_NESTED_MARKER_PREFIXES = ["9. ", "1) ", "# ", "### ", "> ", "--- ", "=== "]
_NBSP_OR_SPACE_RUN = st.integers(min_value=0, max_value=4).map(lambda n: "\u00a0" * n)

_marker_link_fragment = st.builds(
    lambda prefix, ws, words: f"[{prefix}{ws} {' '.join(words)}](file:///C:/old/report.htm#_Toc1)",
    st.sampled_from(_NESTED_MARKER_PREFIXES),
    _NBSP_OR_SPACE_RUN,
    st.lists(st.sampled_from(_TOC_WORDS), min_size=1, max_size=4),
)
_nested_prose_fragment = st.lists(st.sampled_from(_PROSE_WORDS), min_size=1, max_size=5).map(
    " ".join
)
_nested_fragment = st.one_of(_marker_link_fragment, _nested_prose_fragment)


def _make_list_block(items: list[tuple[str, str | None]]) -> tuple[str, int, int, int, bool]:
    lines: list[str] = []
    bullet_lists = 1
    list_items = 0
    for head, sub in items:
        lines.append(f"- {head}")
        list_items += 1
        if sub is not None:
            lines.append(f"  - {sub}")
            list_items += 1
            bullet_lists += 1  # the nested sub-item's own bullet_list_open
    return "\n".join(lines), bullet_lists, 0, list_items, True


_list_item_pair = st.tuples(_nested_fragment, st.one_of(st.none(), _nested_fragment))
_list_block = st.lists(_list_item_pair, min_size=1, max_size=3).map(_make_list_block)
_blockquote_block = _nested_fragment.map(lambda f: (f"> {f}", 0, 1, 0, False))
_blockquote_list_block = _nested_fragment.map(lambda f: (f"> - {f}", 1, 1, 1, False))
_nested_prose_block = _nested_prose_fragment.map(lambda f: (f, 0, 0, 0, False))

_nested_container_block = st.one_of(
    _list_block, _blockquote_block, _blockquote_list_block, _nested_prose_block
)
_nested_page_blocks = st.lists(_nested_container_block, min_size=1, max_size=6)


@given(_nested_page_blocks)
@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_generated_nested_marker_pages_never_drift_structure(
    blocks: list[tuple[str, int, int, int, bool]],
) -> None:
    # Two ADJACENT top-level bullet lists, separated only by a blank line,
    # are real CommonMark: they merge into one loose list (this module's own
    # converter.py documents the same rule) — a plain divider paragraph
    # between any two consecutive top-level-list blocks keeps each block's
    # own expected bullet_list_open count independently true, which is what
    # this property is actually about (marker-drift, not list-merging).
    parts: list[str] = []
    expected_bullet_lists = expected_blockquotes = expected_list_items = 0
    prev_is_list = False
    for text, bl, bq, li, is_list in blocks:
        if is_list and prev_is_list:
            parts.append("(divider)")
        parts.append(text)
        expected_bullet_lists += bl
        expected_blockquotes += bq
        expected_list_items += li
        prev_is_list = is_list
    page = "\n\n".join(parts)

    r = clean_page(page)
    kinds = [t for t, _lvl in _full_block_type_level_sequence(r.text)]
    assert kinds.count("bullet_list_open") == expected_bullet_lists
    assert kinds.count("blockquote_open") == expected_blockquotes
    assert kinds.count("list_item_open") == expected_list_items
    # none of this generator's own container markers are ordered lists or
    # headings, so neither may ever appear — their presence would only ever
    # mean a marker-shaped fragment silently started a real nested block
    assert kinds.count("ordered_list_open") == 0
    assert kinds.count("heading_open") == 0

    r2 = clean_page(r.text)
    assert r2.changes == []


# =============================================================================
# Proofs: the final structural guard's two invariants — (a) never escape a
# marker on a line that was ALREADY that block type in the original parse
# (a heading stays a heading no matter what else on the page diverges), and
# (b) an unrepairable divergence rolls back every escape the repair loop
# made in that attempt rather than leaving a partially-edited document.
# =============================================================================


def test_heading_survives_pipe_table_and_footnote_nbsp_neighborhood() -> None:
    """The exact reported real-page neighbourhood: a GFM pipe table (parsed
    as a plain paragraph by the CommonMark preset — left byte-identical, on
    purpose, since this module doesn't implement GFM tables), a real h6
    heading, a paragraph with a footnote-style file: link immediately
    followed by NBSP, and a bold-first list item. The heading must survive
    completely unescaped; only the footnote link and the NBSP are touched."""
    md, r = _clean("table-heading-footnote-neighborhood.md")
    assert "| Host | Notes |" in r.text  # pipe table untouched
    assert "###### Executive Summary" in r.text  # heading NEVER escaped
    assert "\\###### Executive Summary" not in r.text
    assert "- **9.** **Recommendation**" in r.text  # list item untouched
    assert "\\[9\\]" in r.text  # the footnote link's text, brackets escaped
    assert " " not in r.text  # NBSP normalized
    assert r.unresolved == []
    assert not any(c.rule == R_BLOCK_DRIFT for c in r.changes)  # nothing WAS drift here
    assert_no_file_scheme_remains(r.text)


def test_heading_survives_with_earlier_unrelated_cross_block_repair() -> None:
    """Same neighbourhood, but preceded by an EARLIER, unrelated cross-block
    notice-div repair — the shape most likely to have caused the original
    misalignment bug (deliberate_ranges/orig_deliberate_lines non-empty by
    the time this neighbourhood is reached). The heading still survives,
    and the earlier repair still completes correctly alongside it."""
    md, r = _clean("table-heading-footnote-with-earlier-notice.md")
    assert "- Internal network only" in r.text
    assert "### Scope Notice" in r.text  # the earlier repair's own heading
    assert "###### Executive Summary" in r.text  # THIS heading, never escaped
    assert "\\###### Executive Summary" not in r.text
    assert "\\### Scope Notice" not in r.text
    assert "| Host | Notes |" in r.text
    assert "- **9.** **Recommendation**" in r.text
    assert "\\[9\\]" in r.text
    assert r.unresolved == []
    assert_no_file_scheme_remains(r.text)


def test_block_drift_repair_rolls_back_fully_on_unrepairable_dead_end() -> None:
    """Requirement (b): a top-level paragraph starting with 4+ NBSP
    characters — inert in the ORIGINAL (NBSP isn't marker whitespace) but,
    once hygiene normalizes NBSP to a real space, becomes 4+ leading spaces
    with no enclosing list/blockquote — CommonMark's INDENTED CODE BLOCK,
    which this module documents as the one shape a backslash escape cannot
    undo (there is no escape for "this text has too much leading
    whitespace"). Proof: the final text is BYTE-IDENTICAL to what hygiene
    alone produced (the guard changed nothing at all), and the divergence
    is reported, not silently dropped."""
    NBSP = " "
    md = (
        f"{NBSP * 4}This paragraph used non-breaking spaces for indentation "
        "in the original export.\n"
    )
    pre_guard = (
        "    This paragraph used non-breaking spaces for indentation in the original export.\n"
    )
    r = clean_page(md)
    assert r.text == pre_guard
    assert not any(c.rule == R_BLOCK_DRIFT for c in r.changes)  # nothing kept from the attempt
    assert any("diverged" in i.explanation for i in r.unresolved)
    r2 = clean_page(r.text)
    assert r2.changes == []  # still idempotent even though it's not "fixed"


def test_block_drift_rollback_never_leaves_document_worse_than_pre_guard() -> None:
    """A second, independent shape hitting the same dead end — inside a
    blockquote instead of top-level — proving the guard's per-line "repair
    or leave alone" behaviour generalizes: a line the guard can't safely
    repair (see ``_verify_block_structure``'s per-line ``repair_or_report``)
    is left exactly as hygiene produced it, not partially edited. This one
    test white-box imports the pipeline's own internal stages (unlike every
    other test in this file) specifically to compute "what hygiene alone
    would have produced" as the guard's own expected target on a page with
    only that one, unrepairable line — there is no black-box way to state
    that expectation."""
    from grison.migrate.wiki_cleanup import (
        _convert_structural,
        _hygiene_pass,
        _line_offsets,
        _normalize_crlf,
    )

    NBSP = "\xa0"
    md = f">{NBSP * 5}Quoted text using non-breaking-space indentation.\n"
    text, _ch = _normalize_crlf(md)
    line_map: list[int | None] = list(_line_offsets(text))
    text, _ch2, _un, _img, deliberate, _odl, line_map = _convert_structural(
        text, line_map, own_hosts=frozenset(), image_map={}
    )
    pre_guard, _ch3, _deliberate, _line_map = _hygiene_pass(
        text, line_map, title=None, deliberate_ranges=deliberate
    )

    r = clean_page(md)
    assert r.text == pre_guard


# =============================================================================
# Proofs: the line-map / per-line-signature redesign of the final structural
# guard (replacing whole-document sequence alignment with exclusion ranges,
# which a real-page fourth pass showed could both regress an unrelated,
# already-correct repair and blame the wrong line for a divergence it never
# caused — see the module's final-guard docstring).
# =============================================================================


def test_unrelated_drift_repair_survives_a_separate_unrepairable_dead_end() -> None:
    """The exact regression a real page exposed: an all-or-nothing rollback
    used to discard a perfectly good, already-applied escape (the TOC-style
    link-text marker drift here) just because SOME OTHER, unrelated line on
    the same page hit the one genuinely unrepairable dead end (4+ leading
    NBSP normalizing into an indented code block). Each line's outcome must
    now be independent: the fixable line is fixed, the unfixable one is
    reported, and neither affects the other."""
    md, r = _clean("toc-drift-with-unrelated-dead-end.md")
    assert "- 9\\.     Appendix A - Raw Scan Output" in r.text
    assert "file:" not in r.text
    assert any(c.rule == R_BLOCK_DRIFT for c in r.changes)
    assert len(r.unresolved) == 1
    assert "diverged" in r.unresolved[0].explanation
    assert "    This paragraph used non-breaking spaces" in r.text
    r2 = clean_page(r.text)
    assert r2.changes == []


def test_file_link_stripped_even_when_sibling_link_has_unsupported_scheme() -> None:
    """A `file:` link and a link with a scheme this module refuses to guess
    at (neither http(s)/mailto nor file:/bare) sharing the SAME paragraph
    used to make the whole paragraph refuse conversion — see
    ``_text_leaf_unsupported_reason`` — leaving the file: link un-stripped
    too. The unsupported-scheme construct alone is now left untouched
    (reported, never guessed at) while everything else in the SAME text,
    including an unrelated file: link, is still processed."""
    md, r = _clean("file-link-mixed-with-unsupported-scheme.md")
    assert "file:///" in md
    assert_no_file_scheme_remains(r.text)
    assert "\\[9\\]" in r.text
    assert "<xmpp://old.example.com/room>" in r.text  # never guessed at
    assert any("xmpp" in i.explanation or "scheme" in i.explanation for i in r.unresolved)
    r2 = clean_page(r.text)
    assert r2.changes == []


def test_double_bracket_file_link_wrapped_in_bold_still_stripped() -> None:
    """``[**[9]**](file:///...)`` — the double-bracket footnote-citation
    shape wrapped in emphasis, seen on a real page — could not be located at
    all by reconstructing ``"[" + text + "]"`` from the link's own resolved
    text, since a ``strong_open``/``strong_close`` token pair contributes no
    ``.content`` of its own (see ``_find_matching_bracket``): the file: link
    was silently left completely untouched, with nothing even reported.
    Locating it by matching brackets directly in the source instead finds it
    regardless of nested markup."""
    md, r = _clean("footnote-bold-wrapped-file-link.md")
    assert "file:///" in md
    assert_no_file_scheme_remains(r.text)
    assert "\\[9\\]" in r.text
    assert r.unresolved == []
    r2 = clean_page(r.text)
    assert r2.changes == []


# --- types sanity -------------------------------------------------------------


def test_result_dataclasses_are_frozen_and_hashable() -> None:
    c = Change("rule", 1, "before", "after")
    i = Issue(1, "explanation")
    hash(c)
    hash(i)
