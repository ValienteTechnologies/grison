"""Wiki page BODY hygiene checks (WIKI-005..013 in ``grison/validator/registry.py``).

BookStack takes a page's markdown verbatim — there is no converter (unlike
Ghostwriter's tiny HTML vocabulary) — so these checks parse the body with
markdown-it-py directly and enforce the spec's own rules on top of it, rather than
deferring to any HTML whitelist.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from grison.validator import registry
from grison.validator.registry import Failure

# A fresh instance so overriding validateLink below never affects any other parser
# (grison.markdown.converter's own MarkdownIt instance is a different object).
_MD = MarkdownIt("commonmark")
# markdown-it's built-in validateLink only blocks vbscript:/javascript:/file:/data:
# (and lets ftp:, tel:, arbitrary custom schemes through as real link tokens) — too
# permissive for WIKI-006's own http/https/mailto allow-list, AND too restrictive the
# other way (file: links never even become a `link` token, so this checker would never
# see them to reject). Disabling it makes every URL-shaped destination parse into a
# real link/image token, so WIKI-006 has one uniform place to apply the real rule.
_MD.validateLink = lambda url: True  # type: ignore[method-assign]

# HTML5 element names — a wiki page's raw-HTML check (WIKI-005) only rejects an
# html_block/html_inline token whose tag name is a REAL element; a placeholder like
# <domain>/<user>/<ip> (not a known tag name) reads as prose and must pass.
_KNOWN_HTML_TAGS = frozenset(
    """a abbr address area article aside audio b base bdi bdo blockquote body br button
    canvas caption cite code col colgroup data datalist dd del details dfn dialog div
    dl dt em embed fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header
    hgroup hr html i iframe img input ins kbd label legend li link main map mark menu
    meta meter nav noscript object ol optgroup option output p param picture pre
    progress q rp rt ruby s samp script section select slot small source span strong
    style sub summary sup table tbody td template textarea tfoot th thead time title tr
    track u ul var video wbr""".split()
)
_TAG_NAME_RE = re.compile(r"^</?\s*([a-zA-Z][a-zA-Z0-9-]*)")

_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})

# Zero-width formatting + bidi control characters — never legitimate in prose.
_CONTROL_CHARS = (
    "​‌‍﻿⁠"  # zero-width space/non-joiner/joiner/BOM/word-joiner
    "‎‏"  # LRM/RLM
    "‪‫‬‭‮"  # embedding/override
    "⁦⁧⁨⁩"  # isolates
)
_CONTROL_RE = re.compile("[" + _CONTROL_CHARS + "]")


def _line_of(node: SyntaxTreeNode) -> int | None:
    return (node.map[0] + 1) if node.map else None


def _is_real_html(content: str) -> bool:
    """True if ``content`` (an html_block/html_inline token's raw text) names a REAL
    HTML tag — a comment/doctype/CDATA/processing instruction (no tag-name match at
    all) always counts as real HTML too; only an unknown tag NAME is a placeholder."""
    m = _TAG_NAME_RE.match(content.strip())
    if not m:
        return True
    return m.group(1).lower() in _KNOWN_HTML_TAGS


def parse_body(text: str) -> SyntaxTreeNode:
    return SyntaxTreeNode(_MD.parse(text))


def check_raw_html(path: str, tree: SyntaxTreeNode) -> list[Failure]:
    out: list[Failure] = []
    for node in tree.walk():
        if node.type in ("html_block", "html_inline") and _is_real_html(node.content):
            out.append(
                registry.fail(
                    registry.WIKI_RAW_HTML, path,
                    f"real HTML: {node.content.strip()[:60]!r}", line=_line_of(node),
                )
            )
    return out


def check_link_schemes(path: str, tree: SyntaxTreeNode) -> list[Failure]:
    out: list[Failure] = []
    for node in tree.walk():
        if node.type not in ("link", "image"):
            continue
        href = node.attrs.get("href") if node.type == "link" else node.attrs.get("src")
        if not isinstance(href, str):
            continue
        scheme = urlsplit(href).scheme.lower()
        if scheme and scheme not in _ALLOWED_SCHEMES:
            out.append(
                registry.fail(
                    registry.WIKI_BAD_LINK_SCHEME, path,
                    f"link scheme {scheme!r}: {href!r}", line=_line_of(node),
                )
            )
    return out


def check_control_chars(path: str, text: str) -> list[Failure]:
    out: list[Failure] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for ch in _CONTROL_RE.findall(line):
            out.append(
                registry.fail(
                    registry.WIKI_CONTROL_CHAR, path,
                    f"U+{ord(ch):04X} ({unicodedata.name(ch, 'UNKNOWN')})", line=i,
                )
            )
    return out


def check_line_hygiene(path: str, raw_text: str) -> list[Failure]:
    """CRLF, trailing whitespace, and end-of-file newline — checked against the RAW
    (unnormalized) file text, since these are exactly the byte-level shapes markdown
    itself is blind to."""
    out: list[Failure] = []
    if "\r\n" in raw_text or "\r" in raw_text:
        out.append(registry.fail(registry.WIKI_CRLF, path, "file contains a CR byte"))
    body = raw_text.replace("\r\n", "\n")
    for i, line in enumerate(body.split("\n"), start=1):
        if line != line.rstrip():
            out.append(registry.fail(registry.WIKI_TRAILING_WHITESPACE, path,
                                     "trailing whitespace", line=i))
    if raw_text and not raw_text.endswith("\n"):
        out.append(registry.fail(registry.WIKI_BAD_EOF, path, "no trailing newline"))
    elif raw_text.endswith("\n\n"):
        out.append(registry.fail(registry.WIKI_BAD_EOF, path, "more than one trailing newline"))
    return out


def check_headings(path: str, tree: SyntaxTreeNode, title: str) -> list[Failure]:
    out: list[Failure] = []
    headings = [n for n in tree.walk() if n.type == "heading"]
    prev_level = 0
    for i, node in enumerate(headings):
        level = int(node.tag[1])
        if level > prev_level + 1:
            out.append(
                registry.fail(
                    registry.WIKI_HEADING_SKIP, path,
                    f"heading level {level} follows level {prev_level}", line=_line_of(node),
                )
            )
        prev_level = level
        if i == 0:
            text = node.children[0].content.strip() if node.children else ""
            if text.casefold() == title.strip().casefold():
                out.append(
                    registry.fail(
                        registry.WIKI_TITLE_REPEATED, path,
                        f"first heading repeats the title: {text!r}", line=_line_of(node),
                    )
                )
    return out


def check_page(path: str, file_text: str, body_text: str, title: str) -> list[Failure]:
    """Run every WIKI-005..013 check on one page. ``file_text`` is the WHOLE file
    (frontmatter included) — line-ending/whitespace/control-character hygiene is a
    byte-level property of the file, not of the parsed-and-stripped body.
    ``body_text`` is just the body (post frontmatter, as :mod:`grison.formats.wiki`
    parsed it) — what the markdown-structural checks (raw HTML, link schemes,
    headings) run against."""
    out = list(check_control_chars(path, file_text))
    out.extend(check_line_hygiene(path, file_text))
    tree = parse_body(body_text)
    out.extend(check_raw_html(path, tree))
    out.extend(check_link_schemes(path, tree))
    out.extend(check_headings(path, tree, title))
    return out
