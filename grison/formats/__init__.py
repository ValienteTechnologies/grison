"""Workspace format v2: one strict pydantic model per file type, built on
:mod:`grison.markdown.frontmatter`. Every module exposes ``parse(text, *, path) -> Doc``
and ``dump(doc) -> str`` such that ``dump(parse(x))`` is a fixpoint of ``parse`` (never
necessarily byte-identical to arbitrary hand-written ``x`` — whitespace and key order
canonicalize) and raises :class:`~grison.formats.common.FormatError` naming precisely
what's wrong. See ``docs/workspace-format.md`` for the written spec every rule id in
:mod:`grison.validator` traces back to.

Format v2 documents carry NO machine fields: no ``grison:`` block, no ids, no hashes, no
``evidence:`` list, no ``book``/``chapter`` on wiki pages. Tier/kind/location come from
the path, never the frontmatter.
"""

from __future__ import annotations

from grison.formats.common import NAME_RE, FormatError
from grison.formats.finding import FindingDoc, tier_of
from grison.formats.finding import dump as dump_finding
from grison.formats.finding import parse as parse_finding
from grison.formats.narrative import NarrativeDoc
from grison.formats.narrative import dump as dump_narrative
from grison.formats.narrative import parse as parse_narrative
from grison.formats.note import NoteDoc
from grison.formats.note import dump as dump_note
from grison.formats.note import parse as parse_note
from grison.formats.wiki import WikiPageDoc
from grison.formats.wiki import dump as dump_wiki
from grison.formats.wiki import parse as parse_wiki

__all__ = [
    "NAME_RE",
    "FindingDoc",
    "FormatError",
    "NarrativeDoc",
    "NoteDoc",
    "WikiPageDoc",
    "dump_finding",
    "dump_narrative",
    "dump_note",
    "dump_wiki",
    "parse_finding",
    "parse_narrative",
    "parse_note",
    "parse_wiki",
    "tier_of",
]
