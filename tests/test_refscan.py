"""``grison/markdown/refscan.py`` — the token-stream reference scanner shared between
the validator and (later) the sync engine. These tests exist specifically to prove
the D3 correction: an image inside a fenced code block or an inline code span is NOT
a reference (a regex over raw lines would see it; a real markdown-it token stream
never does), and every real reference reports the right kind/position/line.
"""

from __future__ import annotations

from urllib.parse import quote

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grison.markdown.refscan import decode_ref_path, scan_refs


def test_image_inside_a_fenced_code_block_is_not_a_reference() -> None:
    md = "```\n![not a ref](evidence/fake.png)\n```\n"
    assert scan_refs(md) == []


def test_image_inside_an_inline_code_span_is_not_a_reference() -> None:
    md = "Some prose with `![not a ref](evidence/fake.png)` inline.\n"
    assert scan_refs(md) == []


def test_a_real_embed_after_a_fence_and_a_code_span_is_still_found_with_right_line() -> None:
    md = (
        "```\n"
        "![fake](evidence/fake1.png)\n"
        "```\n"
        "\n"
        "`![fake](evidence/fake2.png)`\n"
        "\n"
        "![Real embed](evidence/real.png \"desc\")\n"
    )
    refs = scan_refs(md)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind == "embed"
    assert ref.path == "evidence/real.png"
    assert ref.caption == "Real embed"
    assert ref.title == "desc"
    assert ref.line == 7
    assert ref.standalone is True
    assert ref.in_list_item is False


def test_standalone_top_level_embed() -> None:
    md = "![Caption](evidence/x.png)\n"
    (ref,) = scan_refs(md)
    assert ref.standalone is True
    assert ref.in_list_item is False
    assert ref.line == 1


def test_standalone_embed_inside_a_list_item() -> None:
    md = "1. Step one.\n2. Step two.\n\n   ![Caption](evidence/x.png)\n"
    refs = [r for r in scan_refs(md) if r.kind == "embed"]
    assert len(refs) == 1
    assert refs[0].standalone is True
    assert refs[0].in_list_item is True


def test_mid_sentence_image_is_not_standalone() -> None:
    md = "See ![this](evidence/x.png) right here.\n"
    (ref,) = scan_refs(md)
    assert ref.kind == "embed"
    assert ref.standalone is False


def test_image_alongside_other_text_in_a_list_item_is_not_standalone() -> None:
    md = "- prefix ![Caption](evidence/x.png) suffix\n"
    (ref,) = scan_refs(md)
    assert ref.standalone is False
    assert ref.in_list_item is True


def test_cross_reference_link_is_kind_cross_reference_never_embed() -> None:
    md = "See [Figure 1](evidence/x.png) for details.\n"
    (ref,) = scan_refs(md)
    assert ref.kind == "cross_reference"
    assert ref.path == "evidence/x.png"
    assert ref.caption == "Figure 1"
    assert ref.standalone is False


def test_cross_reference_inside_a_code_span_is_not_a_reference() -> None:
    md = "Literally `[Figure 1](evidence/x.png)` as text.\n"
    assert scan_refs(md) == []


def test_caption_and_link_text_strip_markup() -> None:
    md = "![**Bold** caption](evidence/x.png)\n\nSee [link *text*](evidence/y.png).\n"
    refs = scan_refs(md)
    embed = next(r for r in refs if r.kind == "embed")
    xref = next(r for r in refs if r.kind == "cross_reference")
    assert embed.caption == "Bold caption"
    assert xref.caption == "link text"


def test_empty_and_blank_markdown_scans_to_nothing() -> None:
    assert scan_refs("") == []
    assert scan_refs("   \n\n  \n") == []


def test_results_are_sorted_by_line() -> None:
    md = "![b](evidence/b.png)\n\nSee [a](evidence/a.png) and ![c](evidence/c.png) too.\n"
    refs = scan_refs(md)
    assert [r.line for r in refs] == sorted(r.line for r in refs)


# --- non-ASCII destinations (markdown-it-py percent-encodes them) ------------------


def test_non_ascii_embed_path_decodes_to_the_authored_spelling() -> None:
    """markdown-it-py's link normalisation percent-encodes non-ASCII bytes in a
    destination — without decode_ref_path, ``ref.path`` would come back as
    ``evidence/Phishing_Sonu%C3%A7lar%C4%B1.png`` even though the author wrote
    (and the file on disk is named) ``evidence/Phishing_Sonuçları.png``."""
    md = "![Results](evidence/Phishing_Sonuçları.png)\n"
    (ref,) = scan_refs(md)
    assert ref.path == "evidence/Phishing_Sonuçları.png"


def test_non_ascii_cross_reference_path_decodes_too() -> None:
    md = "See [the results](evidence/Sonuçları.png) above.\n"
    (ref,) = scan_refs(md)
    assert ref.path == "evidence/Sonuçları.png"


def test_percent_encoded_and_raw_spellings_of_the_same_path_resolve_identically() -> None:
    """A destination the author (or some other tool) already wrote pre-encoded
    resolves to the SAME identity as the raw spelling — decode_ref_path is applied
    uniformly, not conditionally on whether markdown-it happened to encode it."""
    raw_path = "evidence/Sonuçları.png"
    md_raw = f"![x]({raw_path})\n"
    md_encoded = f"![x]({quote(raw_path, safe='/')})\n"
    (ref_raw,) = scan_refs(md_raw)
    (ref_encoded,) = scan_refs(md_encoded)
    assert ref_raw.path == ref_encoded.path == raw_path


def test_decode_ref_path_leaves_invalid_utf8_percent_encoding_undecoded() -> None:
    """A destination whose percent-encoding names bytes that are not valid UTF-8
    is left exactly as written — never raised out of here (every caller of
    scan_refs would have to catch it), and never silently mangled (errors=
    "replace" would turn it into a lossy, misleadingly "valid-looking" string).
    Left alone, it simply never resolves to a real file/index entry, so it
    surfaces as an ordinary, clearly-worded "does not resolve" validation
    failure with the bogus escaped text visible."""
    bogus = "evidence/%ff%fe.png"
    assert decode_ref_path(bogus) == bogus


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs", "Cc", "Co", "Zs", "Zl", "Zp"),
            blacklist_characters="[]()\"`\\<>%",
        ),
        min_size=1,
        max_size=40,
    ).filter(lambda s: s == s.strip() and s != "")
)
def test_decode_ref_path_round_trips_any_unicode_filename(name: str) -> None:
    """Property: whatever unicode text markdown-it hands back for a destination
    ``evidence/<name>``, decoding it recovers exactly ``evidence/<name>`` — the
    round trip an image line embedding an arbitrary unicode filename depends on."""
    path = f"evidence/{name}"
    md = f"![x]({path})\n"
    refs = scan_refs(md)
    assert len(refs) == 1
    assert refs[0].path == path
