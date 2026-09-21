"""The document reconcile engine — a text file mirrors one remote record (a
BookStack page/book/chapter/shelf, today; findings/notes later) through a
:class:`~grison.engine.adapter.Adapter`. This is the "the other" of ENGINE.md's
two reconcile engines, :mod:`grison.engine.filesets` (a folder of bytes) being
the seam.

Split into one file per concern, the same discipline :mod:`grison.engine.filesets`
already uses (round 2 dedup — this used to be one 870-line ``grison/engine/apply.py``):
:mod:`.model` (``RunOptions`` + the small ``_remote_hash`` primitive), :mod:`.classify`
(identity pairing + the per-record classification that produces a
:class:`~grison.engine.model.Plan`), :mod:`.guards` (the pre-write re-fetch guard, the
collision sidecar), :mod:`.apply_remote`/:mod:`.apply_local` (the outcome-specific
writes), :mod:`.dispatch` (the per-plan apply loop), and :mod:`.sync` (the top-level
:func:`run` orchestration). Everything genuinely identical between this engine and
:mod:`grison.engine.filesets` — the change guard, the pre-write re-fetch guard, the
validation gate, the delete-local step, the indexed/present/missing/unindexed
derivation, the apply-loop shell — lives in :mod:`grison.engine.common` instead,
imported by both; what's here is only what a document-shaped record needs that a
file-set record doesn't (read-only/append-only classification, canonicalisation-
after-push, the move/reparent write).
"""

from __future__ import annotations

from .model import RunOptions
from .sync import run

__all__ = ["RunOptions", "run"]
