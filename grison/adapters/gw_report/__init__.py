"""Ghostwriter report directories + narrative sections, on the sync engine
(ENGINE.md Adapter protocol; the rework's brief, reports task).

Two things live here, deliberately NOT the same mechanism:

- **Report directories** (kind ``gw.report``, in :mod:`.dirs`) are a read-only
  STRUCTURE, exactly like :mod:`grison.adapters.bs_structure`'s books/chapters:
  created on pull (``slug(title)``, de-duplicated, never renamed — D3/D4), never
  created or deleted locally, never routed through
  :mod:`grison.engine.classify`/``apply`` themselves. :func:`sync_report_dirs`
  also (re)generates each report's two read-only mirrors, ``.report.yml`` and
  ``project.md`` (:mod:`.mirror`), via the shared
  :func:`grison.engine.mirrors.write_mirror_guarded` guard, and raises the
  missing-scope trip-wire (a project with zero scopes) as an ATTENTION event.
- **Narrative sections** (kind ``gw.reportSection``, in :mod:`.sections`, one
  file per instance-defined ``extraFieldSpec`` row on the Report model) ARE a
  normal, real :class:`~grison.engine.adapter.Adapter`, routed through the ONE
  engine exactly like :class:`grison.adapters.bs_pages.BsPageAdapter` —
  push/pull/collision per section, the same classification table, the same
  pre-write re-fetch guard.

A section has no id of its own in Ghostwriter (``report.extraFields`` is a single
jsonb map on the report row, not a table with rows) — :func:`section_id` gives every
``(report, field)`` pair a distinct, decodable synthetic int id for
``.grison/index.json``/``.grison/state/`` (see its own docstring for the encoding and
why this is a safe, narrow, local choice rather than a data-model fork).

Everything is re-exported here so ``from grison.adapters.gw_report import
NarrativeSectionAdapter`` (and friends) keeps working, and so
``grison.cli.phases.reports``'s ``gw_report.ReportDirResult``/
``gw_report.sync_report_dirs`` module-attribute access keeps working too.
"""

from __future__ import annotations

from .dirs import PROJECT_CONTEXT_FILE, REPORT_META, ReportDirResult, sync_report_dirs
from .mirror import project_context_to_md
from .sections import (
    NARRATIVE_DIR,
    NarrativeSectionAdapter,
    SectionDoc,
    decode_section_id,
    section_id,
)

__all__ = [
    "NARRATIVE_DIR",
    "PROJECT_CONTEXT_FILE",
    "REPORT_META",
    "NarrativeSectionAdapter",
    "ReportDirResult",
    "SectionDoc",
    "decode_section_id",
    "project_context_to_md",
    "section_id",
    "sync_report_dirs",
]
