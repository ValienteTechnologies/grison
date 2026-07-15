"""Fixpoint stress tests for the md<->html converter.

The sync engine's merge base depends on ``rt(x) = pull(push(x))`` never
oscillating: ``rt(rt(x)) == rt(x)`` for every markdown document the finding
vocabulary accepts (a lossy-but-STABLE normalization is fine — an unstable one
would make pull/push loop forever fighting each other). This module mirrors
``grison/remote/gwmap.py``'s push/pull application (outer strip; empty input ->
``""``) locally rather than importing it, so it stays independent of that
module's own test coverage:

    push(x) mirrors finding_to_gw_fields()'s inner ``html()`` helper.
    pull(h) mirrors ``_field_to_md()`` (sans the GW-specific stray-heading
    cleanup in ``clean_gw_html``, which is out of scope for the converter
    itself and never changes any construct exercised here).

Two layers: a hand-picked table of synthetic constructs (including the three
defects fixed alongside this file — ``__strong__``, ``***both***``, and link
titles) and a seeded fuzz pass recombining the same whitelist vocabulary.
"""

from __future__ import annotations

import random

import pytest

from grison.markdown.converter import ConverterError, html_to_md, md_to_html


def push(x: str) -> str:
    if not x.strip():
        return ""
    return md_to_html(x)


def pull(h: str) -> str:
    if not h or not h.strip():
        return ""
    return html_to_md(h).strip()


def rt(x: str) -> str:
    return pull(push(x))


# --- synthetic construct table -----------------------------------------------
# (name, markdown, expect_identity) — expect_identity=False means rt(x) != x is
# allowed (a stable cosmetic normalization, e.g. `_em_` -> `*em*`), but
# rt(rt(x)) == rt(x) is checked unconditionally either way.

FIXPOINT_CASES: list[tuple[str, str, bool]] = [
    ("plain_paragraph", "This is a plain paragraph.", True),
    ("em_star", "*em text*", True),
    ("em_underscore", "_em text_", False),
    ("strong_star", "**strong text**", True),
    ("strong_underscore", "__strong text__", False),
    ("strong_and_em_star", "***bold and italic***", True),
    ("nested_em_in_strong", "**bold with *ital* inside**", True),
    ("nested_strong_in_em", "*ital with **bold** inside*", True),
    ("strong_inside_em_underscore", "_a__b__c_", False),
    ("em_inside_strong_star", "*a**b**c*", True),
    ("adjacent_bold_runs", "**bold1**text**bold2**", True),
    ("intraword_strong_star", "word**bold**word", True),
    ("snake_case_ident", "the variable user_id was set", True),
    ("snake_case_multi", "check user_id, session_token, and api_key_secret", True),
    ("double_underscore_intraword", "snake_case__names stay literal", True),
    ("mid_word_dunder", "a__b__c mid word", True),
    ("four_underscores_chain", "a_b_c_d_e", True),
    ("inline_code", "some `code here` inline", True),
    ("inline_code_special_chars", "run `a | nc -lvp 4444` now", True),
    ("link_basic", "[text](http://example.com)", True),
    ("link_with_title", '[text](http://example.com "Title Text")', True),
    ("link_title_special_chars", '[text](http://example.com/x?y=1 "A Title")', True),
    ("underscore_url_text", "[user_id_lookup](http://example.com/user_id)", True),
    ("link_visible_text_has_strong", "[**bold link text**](http://example.com)", True),
    ("link_url_has_parens", "[text](http://example.com/path(1))", True),
    ("link_empty_text", "[](http://example.com)", True),
    ("link_adjacent_no_space", "[one](http://a.com)[two](http://b.com)", True),
    ("nested_link_in_bold_in_em", "*em with **bold [link](http://x.com) text** inside*", True),
    ("list_nest_2", "- top\n  - sub1\n  - sub2", True),
    ("list_nest_3_collapses", "- top\n  - sub1\n    - subsub1", False),
    ("bullet_star_normalizes", "* item one\n* item two", False),
    (
        "mixed_all",
        "Intro paragraph with **bold**, *em*, `code`, and a "
        '[link](http://example.com "T").\n\n'
        "- bullet one with user_id\n"
        "- bullet two\n"
        "  - nested bullet\n\n"
        "Another paragraph.",
        True,
    ),
    ("unicode_turkish", "İstanbul ışık güç kullanıcı adı ıı İİ", True),
    ("hard_break", "line one\nline two", True),
    ("html_entity_amp", "Tom & Jerry", True),
    ("quote_in_url_title", '[text](http://example.com "has \\"quotes\\" inside")', True),
    ("bold_empty", "****", True),
    ("code_adjacent_bold", "`code`**bold**`more code`", True),
    ("empty_string", "", True),
    ("whitespace_only", "   \n  \n  ", False),
]


@pytest.mark.parametrize("name,md,expect_identity", FIXPOINT_CASES)
def test_synthetic_fixpoint(name: str, md: str, expect_identity: bool) -> None:
    r1 = rt(md)
    r2 = rt(r1)
    assert r2 == r1, f"{name}: NON-FIXPOINT rt(x)={r1!r} rt(rt(x))={r2!r}"
    if expect_identity:
        assert r1 == md, f"{name}: expected identity, got {r1!r}"


# --- whitelist rejections stay rejected (never oscillate: never accepted) ----

REJECTED_CASES = [
    ("heading", "# Heading"),
    ("table", "| A | B |\n| - | - |\n| 1 | 2 |"),
    ("ordered_list", "1. first\n2. second"),
    ("image", "![alt](http://example.com/x.png)"),
]


@pytest.mark.parametrize("name,md", REJECTED_CASES)
def test_rejected_constructs_stay_rejected(name: str, md: str) -> None:
    with pytest.raises(ConverterError):
        push(md)


# --- seeded fuzz over the same whitelist vocabulary --------------------------

WORDS = [
    "user_id", "session_token", "api_key", "the", "quick", "brown", "fox",
    "işık", "İstanbul", "kullanıcı", "café", "naïve",
    "-rwxr-xr-x", "--flag", "-v", "10.0.0.1", "3.5", "CVE-2021-1234",
]

TEMPLATES = [
    lambda w: w,
    lambda w: f"**{w}**",
    lambda w: f"*{w}*",
    lambda w: f"_{w}_",
    lambda w: f"__{w}__",
    lambda w: f"***{w}***",
    lambda w: f"`{w}`",
    lambda w: f"[{w}](http://example.com/{w})",
    lambda w: f'[{w}](http://example.com/{w} "Title")',
    lambda w: f"**{w} *nested* text**",
    lambda w: f"*{w} `code` text*",
]


def _rand_inline(rng: random.Random, depth: int = 0) -> str:
    tpl = rng.choice(TEMPLATES)
    s = tpl(rng.choice(WORDS))
    if depth < 2 and rng.random() < 0.3:
        s += " " + _rand_inline(rng, depth + 1)
    return s


def _rand_paragraph(rng: random.Random) -> str:
    return " ".join(_rand_inline(rng) for _ in range(rng.randint(1, 6)))


def _rand_list_block(rng: random.Random) -> str:
    bullet = rng.choice(["- ", "* "])
    lines = []
    for _ in range(rng.randint(1, 5)):
        lines.append(bullet + _rand_paragraph(rng))
        if rng.random() < 0.4:
            lines.append("  " + bullet + _rand_paragraph(rng))
    return "\n".join(lines)


def _rand_document(rng: random.Random) -> str:
    blocks = [
        _rand_list_block(rng) if rng.random() < 0.4 else _rand_paragraph(rng)
        for _ in range(rng.randint(1, 4))
    ]
    return rng.choice(["\n\n", "\n\n\n", "\r\n\r\n"]).join(blocks)


def test_fuzz_fixpoint_stable() -> None:
    rng = random.Random(1234567)
    failures = []
    for _ in range(300):
        doc = _rand_document(rng)
        try:
            r1 = rt(doc)
        except ConverterError:
            continue  # rejected at push — outside the vocabulary, can't oscillate
        try:
            r2 = rt(r1)
        except ConverterError as e:
            failures.append((doc, r1, f"rt(r1) raised: {e}"))
            continue
        if r2 != r1:
            failures.append((doc, r1, r2))
    assert not failures, failures
