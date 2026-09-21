"""HTML<->markdown converter for the tiny closed vocabulary Ghostwriter's rich-text
fields accept, plus the three special reference/template forms grison layers on top.

Ghostwriter finding fields render a small, fixed subset of HTML (paragraphs,
lists, bold/code/em/links/hard-breaks, plus TinyMCE's cosmetic ``<span>`` highlight
wrapper). grison round-trips those fields against local markdown: anything outside
the whitelist below must fail loudly (:class:`ConverterError`, naming the construct)
rather than degrade silently or get dropped on the floor.

Whitelist (both directions):
  block:  ``<p>`` <-> paragraph, ``<ul><li>`` <-> ``- `` list item,
          ``<ol><li>`` <-> ``1. `` list item (numbered sequentially on emit;
          ``<ol start="N">`` <-> the first item's literal number)
  inline: ``<strong>`` <-> ``**bold**``/``__bold__``, ``<code>`` <-> `` `code` ``
          (a longer backtick fence is used when the code text itself contains a
          run of backticks), ``<em>`` <-> ``*em*``/``_em_``,
          ``<strong><em>`` <-> ``***both***``,
          ``<a href[ title]>`` <-> ``[text](url[ "title"])`` (a literal ``)`` in the
          URL and backslash-escaped punctuation in text both round-trip),
          ``<br>`` <-> a hard line break inside a paragraph (every markdown newline
          in a paragraph — bare, or CommonMark's own ``\\`` / trailing-two-spaces
          hard-break syntax — is treated as one; grison has no soft-break concept).
  ``<span>`` is unwrapped (kept, tag dropped) rather than rejected, since TinyMCE
  wraps highlighted text in it. An ``<ol type="...">`` (a/i/A/I numbering style) is
  dropped the same way — markdown can't represent it — and reported via
  ``on_loss``.

Nesting: inline tokens nest arbitrarily inside bold/em/link text in both
directions. Lists support one level of nesting: a ``<ul>``/``<ol>`` nested inside
an ``<li>`` renders as a 2-space-indented sub-item (``  - `` or ``  N. `` per its
own tag); a list nested inside ONE OF THOSE (three or more levels deep in the
source) collapses into that same single sub-level. ``<ul>`` and ``<ol>`` mix
freely at any level. A list item containing more than one block — GW's real
``<li><p>…</p><p>…</p></li>`` "loose" shape, or a paragraph followed by an evidence
embed (see below) — renders as a blank-line-separated, indented continuation of
the SAME item (CommonMark's own loose-list-item syntax):

    - first paragraph

      second paragraph
    - next item

An item with exactly one paragraph (with or without a nested list) still renders
bare, with no ``<p>`` wrapping, as before. This multi-block support is one level
only — a nested (2nd-level) list item with multiple blocks of its own is outside
this vocabulary.

Special forms (on top of the plain-prose whitelist above), all needing a
:class:`~grison.markdown.refs.RefResolver` passed as ``refs=``; without one, any
of them raises ``ConverterError`` naming what to do instead (pass ``refs=``):

  Embed (D1/D9): an image whose paragraph contains nothing else —
  ``![caption](evidence/file.png "optional description")`` — as a top-level block
  or as its own block inside a list item. On push this becomes Ghostwriter's
  native node ``<div class="richtext-evidence" data-evidence-id="N"></div>``
  (matches the TipTap ``evidence`` node's ``parseHTML``/``renderHTML`` — see
  ``javascript/src/tiptap_gw/evidence.tsx`` in the Ghostwriter source, tag
  v7.2.6). On pull, both that div and the legacy raw text ``{{.FriendlyName}}``
  (its own paragraph; any whitespace around the dot-form's contents is tolerated,
  matching Ghostwriter's own
  ``r"\\{\\{\\s*\\.([^\\{\\}]*?)\\s*\\}\\}"`` regex in
  ``html_rich_text.py``) become the same image line. Neither HTML form carries a
  caption/description — those are the evidence row's own fields, synced
  separately — so ``html_to_md`` gets them from
  :meth:`~grison.markdown.refs.RefResolver.to_local`. A reference the resolver
  can't resolve locally (e.g. not synced down yet) round-trips as an inert,
  visible placeholder instead of failing the document — see "Unresolved
  references" below.

  Cross-reference (D1): a plain link to an evidence path,
  ``[text](evidence/file.png)`` (only a link whose URL starts with ``evidence/``
  is ever treated as one — an ordinary external link is never touched). On push
  this becomes Ghostwriter's native ``<span data-gw-ref-encoded="…"></span>`` —
  an empty, atomic node with NO text content in storage (confirmed against
  ``javascript/src/tiptap_gw/jinja_literal.ts``'s ``JinjaReference`` node: its
  ``renderHTML`` never has a content hole; the "Figure N"-looking text an author
  sees is a live editor node-view, never serialized). The attribute value is
  ``ref`` code-point-hex-encoded, hyphen-joined (``encodeReference``/
  ``decodeReference`` in that file) — grison reuses the referenced evidence's
  ``RemoteRef.name`` as ``ref``. The legacy text form is ``{{.ref name}}`` (same
  dot-syntax regex, dispatched to ``jinja_funcs.ref`` in
  ``ghostwriter/modules/reportwriter/jinja_funcs.py``). Since the native span
  carries no text at all, the markdown link's own text is not load-bearing for
  the HTML round trip; ``html_to_md`` synthesizes it deterministically from the
  resolved evidence's caption (falling back to the path's filename stem), so
  ``html_to_md(md_to_html(x))`` reaches a stable fixed point (matching the
  existing cosmetic-normalization pattern used for e.g. ``_em_`` -> ``*em*``)
  rather than preserving arbitrary author-chosen link text, which the HTML has no
  room for.

  Unresolved references: when :meth:`~grison.markdown.refs.RefResolver.to_local`
  returns ``None`` for a reference recovered from HTML, ``html_to_md`` keeps it —
  visibly and losslessly — as inline code with the reserved ``gw:`` prefix (the
  same namespace used for active template expressions, below) instead of
  reproducing the plain image/link form it can't fill in:
  `` `gw:evidence-ref:id=42` `` for a native div with no local match, or
  `` `gw:evidence-ref:name=SomeName` `` for a legacy dot-form or cross-reference
  with no local match. Each is reported via ``on_loss`` as an unresolved
  reference (chosen over a dedicated callback to keep the interface minimal — the
  message text is what carries the specifics). On push, ``md_to_html`` recognizes
  this reserved form directly (bypassing ``refs`` entirely, since the remote
  identity is already fully known) and re-emits the exact same canonical
  construct: an own-block id marker becomes the native evidence div; an
  own-block name marker (no id available) becomes the legacy ``{{.name}}`` text
  (the only push-able form without an id); an inline name marker becomes the
  native cross-reference span. An inline id marker is invalid (a div is never an
  inline construct) and raises ``ConverterError``.

  Active template expressions (D10): Ghostwriter compiles every rich-text field
  as a Jinja template at export
  (``ghostwriter/modules/reportwriter/base/html_rich_text.py``'s
  ``rich_text_template``). With ``jinja_escape=True`` (the default — set for
  Ghostwriter-bound text; narrative/report fields and finding fields alike),
  ``md_to_html`` neutralizes every literal Jinja delimiter TOKEN — ``{{``,
  ``}}``, ``{%``, ``%}``, ``{#``, ``#}`` — found in plain author text (including
  inside inline code), ONE TOKEN AT A TIME, by replacing it with a Jinja
  string-literal expression that evaluates back to that exact token, e.g.
  ``{%`` becomes ``{{ '{%' }}``, inserted directly as text — no HTML node at
  all. This is deliberately per-token, not pair-based (an earlier design
  matched whole ``{{...}}``/``{%...%}``/``{#...#}`` spans and wrapped each one
  in Jinja's own ``{% raw %}...{% endraw %}`` block tag) — pairing can't
  neutralize a LONE, never-closed opener (author text containing just ``{%``
  with no ``%}`` anywhere else in the field is just as fatal to a real export
  as a matched pair read differently than intended: Jinja's compiler aborts
  the WHOLE report on an unterminated tag), and ``{% raw %}`` has no
  representation for "start a raw block that's never closed" either. Per-token
  substitution sidesteps pairing (and therefore lone openers, nested/
  overlapping delimiters like ``{{ {% }}``, and author text that happens to
  spell out the literal strings ``{% raw %}``/``{% endraw %}`` with no jinja
  intent at all) uniformly, with no special-casing of any shape. Verified
  against a real Ghostwriter 7.2.6 lab server
  (the rework lab proof (``proofs/d10-jinja-escape-lab.md``, kept outside this repo)): an
  unescaped ``{{7*7}}`` silently evaluates on export, an unescaped unknown tag
  (e.g. ``{% debug %}``) aborts the WHOLE report export with a 500 (Jinja
  compiles the entire field as one template before rendering — no per-record
  isolation), and the Jinja string-literal escape form survives a real export
  byte-for-byte, including a lone unclosed opener and literal ``{% raw %}``/
  ``{% endraw %}`` author text. An earlier design wrapped literal braces in a
  ``<div data-gw-jinja-literal="true">`` HTML node instead. Ghostwriter's TipTap
  schema DOES recognize that node (a real ``JinjaLiteral`` node,
  ``javascript/src/tiptap_gw/jinja_literal.ts``, ``content: "block+"``) — that
  assumption in an earlier draft of this module was wrong — but it was still
  abandoned: a block-content node needs the same always-``<p>``-wrapped
  handling as a list item (more machinery, no benefit), and plain text needs no
  schema support at all, in ANY editor, by construction — a strictly simpler,
  independently-proven guarantee for the same job. ``html_to_md`` unwraps
  grison's own ``{{ '<token>' }}`` escape form back to the literal token it
  stands for — literal-brace text round-trips as plain text with no special
  markdown form, since D10 escaping regenerates the wrapper on the next push
  purely from the braces being present.

  Conversely, a genuine ACTIVE (un-wrapped) ``{{ }}``/``{% %}``/``{# #}``
  sequence found in pulled HTML — one Ghostwriter's own Jinja compiler will
  execute at export, whether or not that was intentional — must not be silently
  neutralized into inert literal text by D10 escaping. ``html_to_md`` represents
  each such sequence as inline code with the reserved ``gw:`` prefix,
  e.g. `` `gw:{{ client.name }}` ``, keeping the exact original text between the
  delimiters; ``md_to_html`` recognizes this reserved form and emits the
  un-escaped, active expression directly, bypassing D10 escaping for that token.
  An active expression found INSIDE a ``<code>`` element is, by contrast, always
  treated as literal (D10-escaped on push if it contains braces) — nesting a
  code span inside another is not valid markdown, and an intentionally-active
  expression inside an inline code span is rare enough that grison declines to
  invent a three-way split to preserve it; write it outside code if it must stay
  active.

  A real, unescaped Jinja ``{% raw %}...{% endraw %}`` region found in pulled
  HTML is a THIRD case, distinct from both of the above: never emitted by
  grison's own push any more (the per-token mechanism above replaced the old
  design that used it — see the proof reference below), but real/legacy stored
  data can still carry one, and Jinja's own semantics say everything between
  the tags is unconditionally literal. ``html_to_md`` resolves the whole
  region straight to its plain literal text — never to a ``gw:``
  active-expression marker, and never leaving the ``{% raw %}``/
  ``{% endraw %}`` markers themselves in the markdown, so the very next push
  re-escapes it (if needed) through the ONE per-token mechanism rather than
  reproducing the (now-legacy) raw-wrap shape. This is what keeps the escaping
  mechanism from ever nesting: a Jinja string-literal quoting expression
  (``{{ '{{' }}``) found INSIDE a raw block would be self-defeating (a raw
  block already disables evaluation of everything inside it, so the quoting
  expression itself renders literally, unevaluated, instead of resolving to the
  token it names) — grison's push never produces this shape because it never
  emits ``{% raw %}`` in the first place.

  The rule: an opener and its closer are recognized WHEREVER they fall,
  never only within one HTML text node. ``{% raw %}`` and ``{% endraw %}`` are
  each their own token (never one paired regex), scanned across every leaf of
  text in the WHOLE field, in document order, against one shared, mutable
  "are we inside a raw region right now" flag (``_RawScanState`` /
  ``_split_raw_regions``) — so a region is recognized (a) across an inline tag
  inside one block, e.g. ``<p>a {% raw %}<strong>{{ x }}</strong>{% endraw %}
  b</p>``: the ``<strong>`` stays real bold formatting in the markdown, but its
  content renders literally (never a ``gw:`` marker), same as the plain text on
  either side; and (b) across two separate top-level block elements, e.g. one
  ``<p>`` ending mid-region and the next ``<p>`` carrying the closer: BOTH
  halves render literally (never active), and the still-open region is
  reported via ``on_loss`` once, at the boundary where it's still open,
  distinct from the ordinary "resolved to literal text" message fired once the
  closer is actually found. An opener with no closer anywhere in the rest of
  the field leaves everything after it literal for good — Jinja's own raw
  block, once opened, never re-enables evaluation on its own, and a field
  shaped that way is already un-exportable regardless of what grison renders.

  The legacy evidence forms (``{{.name}}``, ``{{.ref name}}``) are handled above,
  not by the active/literal distinction. Any OTHER dot-form (``{{.caption}}``,
  ``{{.caption name}}``, or a bare ``{{.name}}`` NOT alone in its paragraph) is
  outside grison's vocabulary and raises ``ConverterError`` — naming it and, for
  ``{{.caption...}}`` specifically (the same class as a table: an unsupported
  construct blocking that one record's pull), telling the author what to do
  about it in Ghostwriter's own editor to unblock it.

Loss visibility: every construct dropped or canonicalized on the HTML->markdown
side — TinyMCE ``data-color``/``style`` highlight spans, non-canonical link
``rel``/``target`` values, an ``<ol type>`` numbering style, class/style/other
cosmetic attributes on any allowed tag, and unresolved references (above) — goes
through the optional ``on_loss`` callback, once per dropped/canonicalized
construct, with a human-readable message. It never changes the output, only
makes the drop visible to the caller instead of silent.

Canonical normalizations: ``html_to_md`` never invents visible content, but it
does canonicalize some HTML shapes that have more than one markdown spelling,
so the SAME markdown always comes back out no matter how the equivalent HTML
was built (each is reported via ``on_loss``, and each keeps visible text
identical — see ``tests/test_converter_property.py`` for the properties
proving this). The complete list:

  1. **Adjacent same-tag inline elements merge.** Two directly-adjacent
     ``<strong>``/``<em>``/``<code>`` siblings (with nothing between them, or —
     ``<strong>``/``<em>`` only — separated by nothing but whitespace-only
     text) merge into one element, absorbing any such whitespace into its
     content (``_merge_adjacent_inline``). A plain (non cross-reference)
     ``<span>`` sitting between two same-tag elements does not block this — it
     is transparent for adjacency purposes, since it renders as nothing but
     its own children anyway (``_flatten_transparent_spans``). A
     structurally-empty ``<strong>``/``<em>``/``<code>`` sitting between two
     same-tag elements is dropped first for the same reason
     (``_drop_structurally_empty_inline``) — see normalization 4 below.
  2. **Adjacent same-tag top-level lists merge.** Two adjacent top-level
     ``<ul>``/``<ol>`` blocks of the same tag merge into one — real CommonMark
     reads two markdown list blocks separated only by a blank line back as a
     single loose list on the very next parse regardless of what grison
     itself wrote, so rendering them as two separate blocks would silently
     drift on the next round trip (``_merge_adjacent_top_level_lists``).
  3. **Whitespace moves out of ``<strong>``/``<em>`` boundaries.** Leading/
     trailing whitespace INSIDE a ``<strong>``/``<em>`` tag moves outside the
     ``**``/``*`` delimiters, since CommonMark's emphasis flanking rule
     requires a delimiter run to sit immediately next to non-whitespace
     content to open/close at all (``_wrap_delim``).
  4. **Whitespace-only (or empty) ``<strong>``/``<em>``/``<code>`` is
     dropped.** There is no markdown delimiter that can wrap nothing (or pure
     whitespace) and mean anything different from the whitespace itself —
     any real whitespace is kept as plain text, only the tag is dropped
     (``_wrap_delim``, ``_fence_code``).
  5. **A list item's content is always ``<p>``-wrapped on push.** Matches
     Ghostwriter's real TipTap list-item schema (``ListItem`` node,
     ``content: 'paragraph block*'`` — a block child is required); an item
     with exactly one paragraph and no nested list still round-trips as a
     bare markdown line, with no visible difference.
  6. **A list nested more than one level deep collapses into that one
     sub-level.** Lists support exactly one level of nesting (a 2-space-
     indented sub-item); a THIRD (or deeper) level found in source HTML
     collapses into the same single sub-level rather than being rejected
     (see "Nesting" above).
  7. **Non-ASCII (or otherwise CommonMark-insignificant) whitespace at a
     rendered line's edge is dropped, the same as an ASCII space.** A real
     TipTap editor routinely leaves a stray ``&nbsp;`` run in stored HTML
     (confirmed against a live Ghostwriter 7.2.6 export); keeping it as
     visible markdown content would be self-defeating, since markdown-it's
     own CommonMark paragraph-content parser trims the exact same character
     class (Python's ``str.strip()``, no args) on the very next push — so
     ``_finalize_line`` trims that whole class up front instead of just the
     literal space character, matching what the next round trip does to it
     regardless.
  8. **An empty or whitespace-only top-level ``<p>``/heading block is
     dropped entirely, not kept as an empty markdown block.** A trailing
     ``<p></p>`` or ``&nbsp;``-only paragraph (both real TipTap artifacts —
     normalization 7 above is what makes the SECOND one empty too) would
     otherwise leave a phantom multi-blank-line gap in the ``"\\n\\n".join``
     output; real CommonMark collapses any run of blank lines between
     blocks down to a single boundary on every reparse regardless, so
     grison's own output reflects that collapse from the start
     (``_render_top_level_blocks``) rather than drifting on the next round.

Every one of these is a real, reported normalization — never a silent change —
and every one keeps the converter's own output an immediate fixpoint: pushing
and pulling ``html_to_md``'s own output again always returns byte-identical
markdown, not merely "close" after one more round.

This package is a pure module split of what was one ``converter.py`` file:
``errors``/``grammar``/``nodes``/``mdparse``/``mdtext``/``refs_codec``/``jinja``/
``inline_normalize`` are direction-neutral leaves shared by both directions;
``from_html`` holds :func:`html_to_md` and everything it alone needs;
``to_html`` holds :func:`md_to_html` and everything it alone needs.
"""

from __future__ import annotations

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.from_html import html_to_md
from grison.markdown.converter.to_html import md_to_html

__all__ = ["ConverterError", "html_to_md", "md_to_html"]
