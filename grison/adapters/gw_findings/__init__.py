"""Ghostwriter findings, format v2 (BRIEF task D): library findings (``gw.finding``,
``findings/library/*.md``) and reported findings (``gw.reportedFinding``, ONE FILE
PER FINDING directly inside an indexed ``gw.report`` directory).

Two :class:`~grison.engine.adapter.Adapter` implementations sharing the same
canonicalization helpers (:mod:`.common`). A reported finding's identity comes from
the index (D3) — never from a directory-name prefix; moving a reported finding's
FILE to another report directory is a real MOVE (:mod:`grison.engine.identity`
pairs it across the whole ``gw.reportedFinding`` scope and
:mod:`grison.engine.documents`'s MOVE handling re-parents it, ``reportId`` included,
in the SAME update call the content push would have made — see
:meth:`.reported.GwReportedFindingAdapter.update`). A library <-> report move is NOT a
move at all: a library finding has no ``reportId``/affected_entities/evidence
slot to move INTO, and a reported finding cannot become a template with no
report — BRIEF: "library <-> report moves are CREATE + DELETE" — the validator
already rejects a library finding with `affected_entities` or an evidence
embed (FND-008/REF-005), so grison never needs to guess: a document that moved
from ``findings/library/`` to ``findings/reports/<dir>/`` (or back) is simply
unindexed at its new path (:mod:`grison.engine.identity` only ever pairs within
ONE kind's scope, and library/reportedFinding are different kinds/different
scan roots), so it lands on CREATE, and the old path lands on DELETE_REMOTE —
exactly the "CREATE + DELETE" the brief calls for, with no special-casing here.

Evidence embeds (D1): every prose section goes through
:func:`grison.engine.filesets.canonical_prose`/:func:`grison.markdown.converter.
md_to_html` with a :class:`~grison.adapters._gw_common.IndexRefResolver` built
from the index's ``gw.evidence`` entries for the finding's own report (a
library finding gets no resolver at
all — BRIEF: "a library finding cannot carry affected_entities/evidence" — an
embed found in one is a validator failure (REF-005) that the apply loop's
validation gate turns into INVALID before ``create``/``update`` is ever
called; ``refs=None`` is still safe defense in depth: any surviving embed
raises ``ConverterError`` rather than being silently dropped).

``position`` (Int!, scoped per report + severity band): read and carried
through untouched on every update (the ``_set`` payload never includes it, so
Hasura's partial update leaves it exactly where it was — grison never fights a
human's manual drag-and-drop reorder in Ghostwriter's own UI); a freshly
created reported finding gets the next free position in its own report+severity
band (one more than the current max, 0 if the band is empty) — "append at the
end of the band".

``affected_entities`` (instance-only) is always pushed ``<p>``-wrapped, one
``<br>``-separated line per entry (BRIEF/lab: "plain text breaks Ghostwriter's
docx export") — this field is short, unstructured free text (not the tiny
markdown vocabulary the five prose sections use), so it gets its own small
HTML<->text helpers in :mod:`.common` rather than going through
:mod:`grison.markdown.converter` (a fork, noted in the report: the converter's
grammar has no plain "just wrap it, don't parse markdown" mode, and inventing
one there for a single field seemed like more surface than the one place that
needs it).

``attachFinding`` (evaluated, rejected — BRIEF): Ghostwriter's own
"attach a library finding to a report" action copies an EXISTING library
finding's fields into a new reportedFinding server-side; grison instead always
creates a reportedFinding from whatever LOCAL markdown content is at that path
(the local file, not a library id, is the source of truth for a create) — using
``attachFinding`` would mean grison's own ``create()`` sometimes ignores the
document it was just handed, which is exactly backwards for a tool whose whole
job is "the file is what gets pushed".

Split one file per concern (round 3 dedup — this used to be one 590-line
``grison/adapters/gw_findings.py``): :mod:`.common` (the canonicalization/tag
helpers shared by both adapters), :mod:`.library` (``findings/library/*.md`` <->
``gw.finding``), :mod:`.reported` (a report's reported findings <-> ``gw.
reportedFinding``). Both adapter classes are re-exported here unchanged.
"""

from __future__ import annotations

from .library import GwLibraryFindingAdapter
from .reported import GwReportedFindingAdapter

__all__ = ["GwLibraryFindingAdapter", "GwReportedFindingAdapter"]
