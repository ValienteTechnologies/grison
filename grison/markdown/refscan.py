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
                out.append(_from_image(img, _line_of(node), standalone=True,
                                       in_list_item=in_list_item))
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
            out.append(_from_image(node, _nearest_line(node), standalone=False,
                                   in_list_item=_in_list_item(node)))
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


def _from_image(node: SyntaxTreeNode, line: int, *, standalone: bool,
                in_list_item: bool) -> FoundRef:
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
