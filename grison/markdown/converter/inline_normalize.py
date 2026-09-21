"""Direction-neutral inline-tree normalizations shared by both converter
directions (see :mod:`grison.markdown.converter`'s module docstring,
"Canonical normalizations", items 1, 3 and 4): dropping a structurally-empty
``<strong>``/``<em>``/``<code>``, splicing away a transparent styling
``<span>``, and merging adjacent same-tag inline elements. Reused verbatim on
the markdown->html side too (see ``_md_to_seminode``/``_render_seminode_html``)
so the same merge behavior applies to markdown-it-sourced content as to real
HTML.
"""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.grammar import _GW_REF_ENCODED_ATTR
from grison.markdown.converter.nodes import _Node, _report_dropped_attrs, _report_loss

_MERGEABLE_TAGS = frozenset({"strong", "em", "code"})


def _is_structurally_empty(node: _Node | str) -> bool:
    """True if ``node`` contributes no visible markdown content at all once
    fully collapsed — purely a structural check on the tree (no render, no
    ``RefResolver`` needed), used to strip out-of-the-way "invisible" nodes
    BEFORE the adjacency scan below so two real ``<strong>``/``<em>`` siblings
    separated only by e.g. an empty ``<em></em>`` are still recognized as
    adjacent and merged. Whitespace-only text and further structurally-empty
    ``<strong>``/``<em>`` wrappers count as empty; a truly-empty ``<code>``
    (matching ``_fence_code``'s own empty-string drop — whitespace-only code
    text is real, preserved content, NOT empty) counts as empty too. Anything
    else — a link, image, reference, ``<br>``, real text — is content, so this
    stays conservative and never mistakes it for empty."""
    if isinstance(node, str):
        return node.strip() == ""
    if node.tag in ("strong", "em"):
        return all(_is_structurally_empty(c) for c in node.children)
    if node.tag == "code":
        return not node.children or all(isinstance(c, str) and c == "" for c in node.children)
    return False


def _flatten_structural_text(node: _Node | str) -> str:
    """Concatenate every leaf text string under a structurally-empty
    ``<strong>``/``<em>``/``<code>`` node (all whitespace or nothing, by
    ``_is_structurally_empty``'s contract) — used only to tell "empty" from
    "whitespace-only" in the drop message below."""
    if isinstance(node, str):
        return node
    return "".join(_flatten_structural_text(c) for c in node.children)


def _flatten_transparent_spans(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Splice a plain (non cross-reference) ``<span>`` in place with its own
    (recursively flattened) children before the adjacency scan below, since
    ``_render_inline`` itself later unwraps such a span to nothing but its
    children's rendering anyway — leaving it in the sibling list would
    otherwise block two REAL same-tag elements on either side of it from
    being seen as adjacent, exactly like a structurally-empty wrapper does
    (see ``_is_structurally_empty``), even though this span's own content is
    not empty (``<strong>a</strong><span><strong>b</strong></span>`` must
    merge into one ``<strong>a b</strong>`` just as if the span weren't
    there). A ``<span>`` carrying the cross-reference marker attrs is NOT
    flattened: it renders as one opaque unit (see ``_render_cross_ref_span``),
    never decomposed. This is also now where the "styling span dropped"/
    dropped-attrs diagnostics for a plain span are reported, since a
    flattened span is never seen by ``_render_inline``'s own per-node
    dispatch again."""
    out: list[_Node | str] = []
    for node in nodes:
        if (
            isinstance(node, _Node)
            and node.tag == "span"
            and _GW_REF_ENCODED_ATTR not in node.attrs
            and "data-gw-ref" not in node.attrs
        ):
            attrs = {k: v for k, v in node.attrs.items() if v and k != "_dropped"}
            if attrs:
                shown = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
                _report_loss(on_loss, f"styling span dropped ({shown})")
            _report_dropped_attrs(node, on_loss)
            out.extend(_flatten_transparent_spans(node.children, on_loss))
            continue
        out.append(node)
    return out


def _drop_structurally_empty_inline(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Remove the TAG from top-level ``<strong>``/``<em>``/``<code>`` siblings
    that are structurally empty (see ``_is_structurally_empty``), reporting
    each one via ``on_loss`` exactly as ``_wrap_delim``/``_fence_code`` would
    at render time — this runs first specifically so the adjacency merge
    below sees real content as truly adjacent. Any whitespace text the
    dropped tag was wrapping is kept as a plain sibling in its place (just
    like ``_wrap_delim`` returns its whitespace ``content`` unwrapped rather
    than deleting it — a bare ``<strong> </strong>`` between two words is
    still a real space separating them, not nothing). Anything that slips
    through this structural check (e.g. a wrapper that renders empty only
    after a reference resolves) is still caught later by ``_wrap_delim``/
    ``_fence_code`` themselves as a safety net."""
    kept: list[_Node | str] = []
    for node in nodes:
        is_mergeable_empty = (
            isinstance(node, _Node)
            and node.tag in ("strong", "em", "code")
            and _is_structurally_empty(node)
        )
        if is_mergeable_empty:
            assert isinstance(node, _Node)
            if node.tag == "code":
                # an empty <code> already renders to "" silently (see
                # _fence_code) with nothing meaningful to report beyond that
                continue
            text = _flatten_structural_text(node)
            rendered_kind = "whitespace-only" if text else "empty"
            _report_loss(
                on_loss, f"{rendered_kind} <{node.tag}> dropped (no markdown representation)"
            )
            if text:
                kept.append(text)
            continue
        kept.append(node)
    return kept


def _merge_dropped_attrs(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    merged = dict(a)
    b_dropped = b.get("_dropped")
    if b_dropped:
        merged["_dropped"] = (merged.get("_dropped", "") + " " + b_dropped).strip()
    return merged


def _coalesce_adjacent_strings(nodes: list[_Node | str]) -> list[_Node | str]:
    """Merge consecutive plain-text siblings into one string. Dropping a
    structurally-empty element (see ``_drop_structurally_empty_inline``) that
    sat between two whitespace text nodes leaves them directly adjacent in
    the list; the one-whitespace-node lookahead in the merge loop below only
    ever looks past a SINGLE text sibling, so without this they'd stay two
    separate list entries and silently double the whitespace between two
    real elements that should otherwise merge across it."""
    out: list[_Node | str] = []
    for node in nodes:
        if isinstance(node, str) and out and isinstance(out[-1], str):
            out[-1] = out[-1] + node
        else:
            out.append(node)
    return out


def _merge_adjacent_inline(
    nodes: list[_Node | str], on_loss: Callable[[str], None] | None
) -> list[_Node | str]:
    """Normalize the HTML tree BEFORE rendering: two directly-adjacent
    ``<strong>``/``<em>``/``<code>`` siblings of the same tag — with nothing
    between them, or (``<strong>``/``<em>`` only) separated by nothing but
    whitespace-only text — merge into ONE element (absorbing any such
    whitespace into its content), reported via ``on_loss`` as a normalization.

    This is the real fix for the ambiguity two adjacent same-delimiter runs
    create on the next markdown parse (``**a**`` + ``**b**`` naively
    concatenated is ``**a****b**``, which CommonMark reads as one ``<strong>``
    spanning ``a****b``, not two) — merging the SOURCE TREE first means
    ``_render_inline`` only ever renders ONE real ``<strong>a b</strong>``,
    visually identical to the two-element original, with no invented
    characters of any kind needed in the output.

    A structurally-empty ``<strong>``/``<em>``/``<code>`` sibling sitting
    between two same-tag elements (e.g. an author-written ``<em></em>``
    between two ``<strong>``s) is dropped FIRST (see
    ``_drop_structurally_empty_inline``), so it can never block this merge —
    otherwise the two ``<strong>``s would render unmerged here but merge
    anyway on the very next round trip (once the empty ``<em>`` has no HTML
    representation left to round-trip through), breaking the fixpoint
    property. A plain (non cross-reference) ``<span>`` sibling is similarly
    spliced away first by ``_flatten_transparent_spans`` — it renders as
    nothing but its own children anyway, so it must not block adjacency
    either."""
    nodes = _flatten_transparent_spans(nodes, on_loss)
    nodes = _coalesce_adjacent_strings(_drop_structurally_empty_inline(nodes, on_loss))
    merged: list[_Node | str] = []
    i = 0
    n = len(nodes)
    while i < n:
        node = nodes[i]
        if isinstance(node, _Node) and node.tag in _MERGEABLE_TAGS:
            combined = node
            j = i + 1
            while j < n:
                nxt = nodes[j]
                if isinstance(nxt, _Node) and nxt.tag == combined.tag:
                    combined = _Node(
                        combined.tag,
                        _merge_dropped_attrs(combined.attrs, nxt.attrs),
                        list(combined.children) + list(nxt.children),
                    )
                    _report_loss(
                        on_loss,
                        f"adjacent <{combined.tag}> elements merged into one (normalization)",
                    )
                    j += 1
                    continue
                if (
                    combined.tag in ("strong", "em")
                    and isinstance(nxt, str)
                    and nxt.strip() == ""
                    and j + 1 < n
                    and isinstance(nodes[j + 1], _Node)
                    and nodes[j + 1].tag == combined.tag  # type: ignore[union-attr]
                ):
                    after = nodes[j + 1]
                    assert isinstance(after, _Node)
                    combined = _Node(
                        combined.tag,
                        _merge_dropped_attrs(combined.attrs, after.attrs),
                        [*combined.children, nxt, *after.children],
                    )
                    _report_loss(
                        on_loss,
                        f"adjacent <{combined.tag}> elements merged into one (normalization)",
                    )
                    j += 2
                    continue
                break
            merged.append(combined)
            i = j
        else:
            merged.append(node)
            i += 1
    return merged
