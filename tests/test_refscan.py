"""``grison/markdown/refscan.py`` — the token-stream reference scanner shared between
the validator and (later) the sync engine. These tests exist specifically to prove
the D3 correction: an image inside a fenced code block or an inline code span is NOT
a reference (a regex over raw lines would see it; a real markdown-it token stream
never does), and every real reference reports the right kind/position/line.
"""

from __future__ import annotations

from grison.markdown.refscan import scan_refs


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
