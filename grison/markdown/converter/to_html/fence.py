"""markdown->html: fenced code blocks (module docstring, "Special forms"/fence)
— the canonical push shape is ``<pre spellcheck="false"><code>escaped
text</code></pre>``, with ``class="language-<info>"`` on ``<code>`` when the
fence has an info string. Content is never inline-parsed (a fence's ``.content``
is markdown-it's own literal, unescaped source text, exactly the promise
``preserveWhitespace: "full"`` makes on the TipTap side) — only HTML-escaped
and, like any other ``<code>`` content, D10-escaped if requested (module
docstring, "Active template expressions": applied identically whether the
fragment sits in plain text or inside a ``<code>`` element — Jinja's lexer
scans the whole HTML string as one template regardless of what tag a
substring sits inside)."""

from __future__ import annotations

from markdown_it.tree import SyntaxTreeNode

from grison.markdown.converter.jinja import _jinja_escape_html
from grison.markdown.converter.mdtext import _esc


def _render_fence_node(node: SyntaxTreeNode, *, jinja_escape: bool) -> str:
    # Only the info string's first word becomes the language token — GW/TipTap's
    # own language picker only ever produces a single bare word, and using the
    # whole raw info string verbatim could inject a second, bogus class token
    # into the HTML class attribute (e.g. info "python extra" -> two classes).
    lang = (node.info or "").strip().split()[0] if node.info and node.info.strip() else ""
    lang_attr = f' class="language-{_esc(lang)}"' if lang else ""
    text = _esc(node.content)
    if jinja_escape:
        text = _jinja_escape_html(text)
    return f'<pre spellcheck="false"><code{lang_attr}>{text}</code></pre>'
