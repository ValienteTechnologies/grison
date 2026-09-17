"""One-time markdown-body migration: re-derive an existing workspace's markdown
bodies so they read back correctly under the current (real-CommonMark-parsing)
converter.

Why this exists: before the markdown-it-py rewrite, ``html_to_md`` (the frozen
copy in :mod:`grison.migrate._v1_converter`, hereafter "v1") didn't always
produce valid CommonMark — a real CommonMark parser (which the current converter
now is) reads some of its output differently than v1 itself would read it back,
or rejects it outright. Two concrete, verified shapes (see the test suite and
``tests/`` for the exact cases):

- ``**Label: **[text](url)`` — v1's regex tokenizer wraps ``**...**`` around text
  ending in a space with no regard for CommonMark's emphasis right-flanking rule
  (a closing ``**`` immediately after a space isn't "left-flanking" and doesn't
  close real emphasis). Read by a real CommonMark parser, the ``**`` marks are
  literal asterisks, not a `<strong>` run — the current converter reads it as
  literal text, not a formatting loss, but that's not what v1 (and Ghostwriter's
  editor) actually meant by it.
- A literal ``<iframe>``/``<script>``/``<domain>``-shaped run of text, or a bare
  ``a < b`` comparison — v1's tokenizer never recognized inline HTML at all, so
  any ``<...>``-looking text was always just HTML-escaped literal content on
  push. The current converter's real CommonMark inline parser recognizes
  ``<iframe...>``-shaped text as inline HTML syntax and hard-rejects it (D1
  "still rejected: raw HTML blocks and inline HTML") when fed the RAW v1
  markdown directly.

Both are the OLD CONVERTER'S OUTPUT SHAPE being non-canonical for the new
converter — not a defect in what the author actually wrote, and not something
:func:`migrate_body_v1` needs to "fix" in the markdown; it needs to reconstruct
what the ORIGINAL html_to_md pull actually produced, then re-read THAT (real
HTML parsing was never the defective side — only v1's md_to_html regex
tokenizer was) with the current converter, which understands the html_to_md
side (an HTMLParser tree, unchanged in approach across the rewrite) correctly.

migrate_body_v1(x) = new.html_to_md(v1.md_to_html(x))

Documented, verified normalizations (see ``tests/test_migrate_bodies.py`` for the
exact cases these were checked against): comparing ``new.md_to_html(migrate_body_v1(x))``
to ``v1.md_to_html(x)`` for the same v1 markdown ``x``, the VISIBLE TEXT (every
tag stripped, entities decoded, concatenated in document order) is always
identical — the two classes of structural difference below never change what a
reader sees, only which tag or text node a character/space sits in:

1. **Whitespace moves out of `<strong>`/`<em>` boundaries.** v1's regex
   tokenizer wraps ``**``/``*`` around text with no regard for CommonMark's
   emphasis right/left-flanking rule (a delimiter immediately adjacent to
   whitespace isn't "flanking" and can't open/close real emphasis). E.g.
   ``**Label: **`` (a bold run ending in a space right before the closing
   ``**``) becomes, after migration, ``**Label:**`` with the trailing space
   moved to sit right after the closing delimiter — so
   ``<strong>Label: </strong>`` (old; the space is the last character INSIDE
   the tag) becomes ``<strong>Label:</strong> `` (new; the same space is now a
   separate text node right AFTER the tag). Concatenated visible text is
   identical either way.
2. **Every list item becomes `<p>`-wrapped.** v1 could emit a bare
   ``<li>text</li>`` (no ``<p>`` at all); the current converter's canonical push
   form always wraps list-item content in ``<p>`` (matching Ghostwriter's real
   TipTap list-item schema, which requires a block child — see
   ``grison/markdown/converter.py``'s module docstring). A ``<p>`` tag
   contributes no text of its own, so this never changes visible text either.

The current converter's own ``html_to_md`` (see its module docstring's full
list of canonical normalizations) is also, separately, always an immediate
fixpoint on the markdown it re-derives here — three of its normalizations in
particular were only found and fixed by running ``migrate_body_v1`` against a
real, production-shaped workspace: adjacent same-tag ``<ul>``/``<ol>`` blocks
(which v1 could emit as two separate list blocks for what one real CommonMark
parse reads back as a single loose list) merge into one; a trailing space
right before an embedded hard break is stripped (real CommonMark trims a
single trailing space as insignificant there, so leaving it in would be
silently dropped on the very next round anyway); and leading whitespace
inside a list item's own text is stripped for the same reason list-item
paragraphs already were. None of these change visible text either — see
``grison/markdown/converter.py`` for the authoritative, complete list.

Nothing else changes silently: anything else is not "migrated," it fails loudly
(see :class:`BodyMigrationError`).
"""

from __future__ import annotations

from grison.errors import GrisonError
from grison.markdown.converter import ConverterError as _NewConverterError
from grison.markdown.converter import html_to_md as _new_html_to_md
from grison.migrate._v1_converter import ConverterError as _V1ConverterError
from grison.migrate._v1_converter import md_to_html as _v1_md_to_html


class BodyMigrationError(GrisonError):
    """A workspace body couldn't be migrated through the old->new converter
    pipeline — wraps whichever side (the old md_to_html re-derivation, or the
    new html_to_md re-read) actually failed, with that error's own message."""


def migrate_body_v1(md: str, *, headings: bool = False) -> str:
    """Re-derive one v1 (pre-rework) markdown body into its current-converter
    equivalent: ``new.html_to_md(v1.md_to_html(md))``.

    ``headings=True`` for report-narrative bodies (extraFields sections), same
    flag both converters already take. Empty/whitespace-only input returns ``""``
    without invoking either converter (matches both converters' own behavior for
    empty input).

    Raises :class:`BodyMigrationError` if either side fails — the old
    re-derivation (meaning this markdown was never actually valid v1 output, e.g.
    hand-edited into something v1's own md_to_html would have rejected) or the
    new re-read (meaning the re-derived HTML is itself outside the current
    converter's vocabulary — should not happen for genuine v1 output; if it does,
    that's a real gap to report, not something to paper over silently). Never
    returns a partially-migrated or best-effort result.
    """
    if not md.strip():
        return ""
    try:
        html = _v1_md_to_html(md, headings=headings)
    except _V1ConverterError as e:
        raise BodyMigrationError(
            f"migration failed: could not re-derive HTML from the existing markdown body "
            f"with the old converter ({e})"
        ) from e
    try:
        return _new_html_to_md(html, headings=headings)
    except _NewConverterError as e:
        raise BodyMigrationError(
            f"migration failed: the re-derived HTML could not be re-read by the current "
            f"converter ({e})"
        ) from e
