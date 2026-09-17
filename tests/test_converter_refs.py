"""Tests for the embed/cross-reference special forms (brief D1/D9, item 2) —
Ghostwriter's native evidence div, its legacy dot-syntax text forms, and its
cross-reference span, both directions, plus unresolved-reference handling."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from grison.markdown.converter import ConverterError, html_to_md, md_to_html
from grison.markdown.refs import LocalRef, RemoteRef


@dataclass
class FakeResolver:
    """A trivial in-memory RefResolver for tests — real implementations do
    filesystem/index lookups; this just looks things up in dicts the test sets up."""

    by_path: dict[str, RemoteRef]
    by_remote: dict[tuple[str, int | None, str | None], LocalRef]

    def to_remote(self, path: str) -> RemoteRef | None:
        return self.by_path.get(path)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        return self.by_remote.get((remote.kind, remote.id, remote.name))


def _resolver(**kw) -> FakeResolver:
    return FakeResolver(by_path=kw.get("by_path", {}), by_remote=kw.get("by_remote", {}))


# --- embed: native div --------------------------------------------------------


def test_embed_push_native_div() -> None:
    refs = _resolver(by_path={"evidence/shot.png": RemoteRef("gw-evidence", 42, None, None)})
    html = md_to_html('![Caption](evidence/shot.png "A description")', refs=refs)
    assert html == '<div class="richtext-evidence" data-evidence-id="42"></div>'


def test_embed_pull_native_div() -> None:
    refs = _resolver(
        by_remote={
            ("gw-evidence", 42, None): LocalRef(
                path="evidence/shot.png", caption="Caption", description="A description"
            )
        }
    )
    md = html_to_md('<div class="richtext-evidence" data-evidence-id="42"></div>', refs=refs)
    assert md == '![Caption](evidence/shot.png "A description")'


def test_embed_pull_native_div_no_description() -> None:
    refs = _resolver(
        by_remote={("gw-evidence", 7, None): LocalRef(path="evidence/x.png", caption="Cap")}
    )
    md = html_to_md('<div class="richtext-evidence" data-evidence-id="7"></div>', refs=refs)
    assert md == "![Cap](evidence/x.png)"


def test_embed_round_trips_through_html_and_back() -> None:
    refs = _resolver(
        by_path={"evidence/shot.png": RemoteRef("gw-evidence", 42, None, None)},
        by_remote={("gw-evidence", 42, None): LocalRef(path="evidence/shot.png", caption="Cap")},
    )
    md = "![Cap](evidence/shot.png)"
    html = md_to_html(md, refs=refs)
    assert html_to_md(html, refs=refs) == md


def test_embed_in_list_item_own_block() -> None:
    refs = _resolver(
        by_path={"evidence/y.png": RemoteRef("gw-evidence", 9, None, None)},
        by_remote={("gw-evidence", 9, None): LocalRef(path="evidence/y.png", caption="Y")},
    )
    html = (
        '<ul><li><p>context</p>'
        '<div class="richtext-evidence" data-evidence-id="9"></div></li></ul>'
    )
    md = html_to_md(html, refs=refs)
    assert md == "- context\n\n  ![Y](evidence/y.png)"
    assert html_to_md(md_to_html(md, refs=refs), refs=refs) == md


def test_embed_push_requires_refs() -> None:
    with pytest.raises(ConverterError):
        md_to_html("![Cap](evidence/x.png)")


def test_embed_pull_requires_refs() -> None:
    with pytest.raises(ConverterError):
        html_to_md('<div class="richtext-evidence" data-evidence-id="1"></div>')


def test_embed_push_unresolvable_path_raises() -> None:
    refs = _resolver()
    with pytest.raises(ConverterError):
        md_to_html("![Cap](evidence/nope.png)", refs=refs)


def test_unsupported_div_shape_raises() -> None:
    with pytest.raises(ConverterError):
        html_to_md('<div class="something-else"></div>', refs=_resolver())


def test_evidence_div_with_content_raises() -> None:
    with pytest.raises(ConverterError):
        html_to_md(
            '<div class="richtext-evidence" data-evidence-id="1">x</div>', refs=_resolver()
        )


# --- embed: legacy dot-syntax text --------------------------------------------


def test_legacy_dot_form_embed_pull() -> None:
    refs = _resolver(
        by_remote={("gw-evidence", None, "Shot1"): LocalRef(path="evidence/s1.png", caption="S1")}
    )
    assert html_to_md("<p>{{.Shot1}}</p>", refs=refs) == "![S1](evidence/s1.png)"


@pytest.mark.parametrize(
    "html",
    [
        "<p>{{.Shot1}}</p>",
        "<p>{{ .Shot1}}</p>",
        "<p>{{.Shot1 }}</p>",
        "<p>{{  .Shot1  }}</p>",
        "<p>{{\n.Shot1\n}}</p>",
    ],
)
def test_legacy_dot_form_tolerates_ghostwriters_whitespace_variants(html: str) -> None:
    # Matches Ghostwriter's own regex: r"\{\{\s*\.([^\{\}]*?)\s*\}\}"
    refs = _resolver(
        by_remote={("gw-evidence", None, "Shot1"): LocalRef(path="evidence/s1.png", caption="S1")}
    )
    assert html_to_md(html, refs=refs) == "![S1](evidence/s1.png)"


def test_legacy_dot_form_bare_name_mid_sentence_rejected() -> None:
    # The embed form is only recognized when it's the paragraph's SOLE content
    # (matches the authored image line's own position rule).
    with pytest.raises(ConverterError):
        html_to_md("<p>See {{.Shot1}} above.</p>", refs=_resolver())


def test_legacy_caption_form_rejected() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<p>{{.caption}}</p>", refs=_resolver())
    with pytest.raises(ConverterError):
        html_to_md("<p>{{.caption Shot1}}</p>", refs=_resolver())


def test_legacy_caption_form_rejection_message_tells_author_what_to_do() -> None:
    # Same class as a table: an unsupported construct that blocks pulling that
    # ONE record — the message must say what to do about it in Ghostwriter's
    # own editor, not just name the construct.
    with pytest.raises(ConverterError) as exc_info:
        html_to_md("<p>{{.caption Shot1}}</p>", refs=_resolver())
    message = str(exc_info.value)
    assert "{{.caption Shot1}}" in message
    assert "Ghostwriter" in message
    assert "editor" in message


# --- cross-reference: native span --------------------------------------------


def test_cross_ref_push_native_span() -> None:
    refs = _resolver(
        by_path={"evidence/shot.png": RemoteRef("gw-evidence", 42, "shot-friendly", None)}
    )
    html = md_to_html("[see figure](evidence/shot.png)", refs=refs)
    # encodeReference("shot-friendly"): each char's code point as lowercase hex,
    # hyphen-joined (javascript/src/tiptap_gw/jinja_literal.ts). A cross-reference
    # is an INLINE construct (unlike an embed), so it still sits inside a <p>.
    encoded = "-".join(format(ord(c), "x") for c in "shot-friendly")
    assert html == f'<p><span data-gw-ref-encoded="{encoded}"></span></p>'


def test_cross_ref_pull_native_span() -> None:
    encoded = "-".join(format(ord(c), "x") for c in "shot-friendly")
    refs = _resolver(
        by_remote={
            ("gw-evidence", None, "shot-friendly"): LocalRef(
                path="evidence/shot.png", caption="My Caption"
            )
        }
    )
    md = html_to_md(f'<p>See <span data-gw-ref-encoded="{encoded}"></span>.</p>', refs=refs)
    assert md == "See [My Caption](evidence/shot.png)."


def test_cross_ref_pull_falls_back_to_path_stem_when_no_caption() -> None:
    encoded = "-".join(format(ord(c), "x") for c in "ref1")
    refs = _resolver(
        by_remote={("gw-evidence", None, "ref1"): LocalRef(path="evidence/shot-1.png")}
    )
    md = html_to_md(f'<p><span data-gw-ref-encoded="{encoded}"></span></p>', refs=refs)
    assert md == "[shot-1](evidence/shot-1.png)"


def test_cross_ref_native_span_with_content_raises() -> None:
    with pytest.raises(ConverterError):
        html_to_md('<p><span data-gw-ref-encoded="61">x</span></p>', refs=_resolver())


def test_cross_ref_round_trips() -> None:
    refs = _resolver(
        by_path={"evidence/shot.png": RemoteRef("gw-evidence", 42, "shot-friendly", None)},
        by_remote={
            ("gw-evidence", None, "shot-friendly"): LocalRef(
                path="evidence/shot.png", caption="Cap"
            )
        },
    )
    html = md_to_html("[whatever text](evidence/shot.png)", refs=refs)
    md1 = html_to_md(html, refs=refs)
    assert md1 == "[Cap](evidence/shot.png)"
    # not necessarily == original text (the native span has no text of its own —
    # see module docstring) but stable after this one normalizing round:
    assert html_to_md(md_to_html(md1, refs=refs), refs=refs) == md1


def test_cross_ref_only_triggered_for_evidence_prefixed_links() -> None:
    # An ordinary external link is never touched, refs or not.
    assert md_to_html("[x](https://example.com/)") == (
        '<p><a href="https://example.com/" target="_blank" rel="noopener">x</a></p>'
    )


def test_cross_ref_push_requires_refs() -> None:
    with pytest.raises(ConverterError):
        md_to_html("[x](evidence/shot.png)")


def test_cross_ref_pull_requires_refs() -> None:
    with pytest.raises(ConverterError):
        html_to_md('<p><span data-gw-ref-encoded="61"></span></p>')


# --- cross-reference: legacy dot-syntax `.ref name` ---------------------------


def test_legacy_ref_form_pull() -> None:
    refs = _resolver(
        by_remote={
            ("gw-evidence", None, "shot-friendly"): LocalRef(
                path="evidence/shot.png", caption="Cap"
            )
        }
    )
    md = html_to_md("<p>See {{.ref shot-friendly}} above.</p>", refs=refs)
    assert md == "See [Cap](evidence/shot.png) above."


def test_legacy_ref_form_requires_refs() -> None:
    with pytest.raises(ConverterError):
        html_to_md("<p>See {{.ref x}} above.</p>")


# --- unresolved references -----------------------------------------------------


def test_unresolved_native_div_becomes_visible_placeholder_and_reports_loss() -> None:
    events: list[str] = []
    refs = _resolver()  # empty — nothing resolves
    md = html_to_md(
        '<div class="richtext-evidence" data-evidence-id="42"></div>',
        refs=refs,
        on_loss=events.append,
    )
    assert md == "`gw:evidence-ref:id=42`"
    assert any("unresolved reference" in e and "42" in e for e in events)


def test_unresolved_native_div_placeholder_pushes_back_to_same_div() -> None:
    refs = _resolver()
    md = "`gw:evidence-ref:id=42`"
    html = md_to_html(md, refs=refs)
    assert html == '<div class="richtext-evidence" data-evidence-id="42"></div>'


def test_unresolved_legacy_name_becomes_placeholder() -> None:
    events: list[str] = []
    md = html_to_md("<p>{{.Shot1}}</p>", refs=_resolver(), on_loss=events.append)
    assert md == "`gw:evidence-ref:name=Shot1`"
    assert any("unresolved reference" in e and "Shot1" in e for e in events)


def test_unresolved_legacy_name_placeholder_pushes_back_to_legacy_text() -> None:
    html = md_to_html("`gw:evidence-ref:name=Shot1`", refs=_resolver())
    assert html == "<p>{{.Shot1}}</p>"


def test_unresolved_cross_ref_becomes_inline_placeholder() -> None:
    events: list[str] = []
    encoded = "-".join(format(ord(c), "x") for c in "x")
    md = html_to_md(
        f'<p>See <span data-gw-ref-encoded="{encoded}"></span> above.</p>',
        refs=_resolver(),
        on_loss=events.append,
    )
    assert md == "See `gw:evidence-ref:name=x` above."
    assert any("unresolved reference" in e for e in events)


def test_unresolved_cross_ref_placeholder_pushes_back_to_same_span() -> None:
    encoded = "-".join(format(ord(c), "x") for c in "x")
    html = md_to_html("See `gw:evidence-ref:name=x` above.", refs=_resolver())
    assert html == f'<p>See <span data-gw-ref-encoded="{encoded}"></span> above.</p>'


def test_unresolved_id_marker_inline_is_rejected() -> None:
    # An id-based marker is only ever produced own-block (a div is never inline).
    with pytest.raises(ConverterError):
        md_to_html("text `gw:evidence-ref:id=1` more text", refs=_resolver())


def test_unresolved_placeholder_round_trip_is_stable() -> None:
    refs = _resolver()
    once = html_to_md(
        '<div class="richtext-evidence" data-evidence-id="1"></div>', refs=refs
    )
    twice = html_to_md(md_to_html(once, refs=refs), refs=refs)
    assert twice == once


# --- position validation -------------------------------------------------------


def test_image_mid_sentence_raises() -> None:
    refs = _resolver(by_path={"evidence/x.png": RemoteRef("gw-evidence", 1, None, None)})
    with pytest.raises(ConverterError):
        md_to_html("before ![x](evidence/x.png) after", refs=refs)


def test_image_inside_link_text_raises() -> None:
    refs = _resolver(by_path={"evidence/x.png": RemoteRef("gw-evidence", 1, None, None)})
    with pytest.raises(ConverterError):
        md_to_html("[![x](evidence/x.png)](http://example.com/)", refs=refs)


# --- RemoteRef/LocalRef data model ---------------------------------------------


def test_remote_ref_accepts_bs_image_kind() -> None:
    # D9's wiki-image kind — not produced/consumed by this converter's HTML side
    # (BookStack pages are markdown-native), but the type must allow it.
    ref = RemoteRef(kind="bs-image", id=3, name="diagram.png", url=None)
    assert ref.kind == "bs-image"


def test_local_ref_defaults() -> None:
    local = LocalRef(path="evidence/x.png")
    assert local.caption == ""
    assert local.description == ""
