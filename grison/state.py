"""grison's private sync-state store — the single source of truth for merge bases and
per-image evidence bookkeeping, kept OUT of the git-tracked content files.

The bug this closes: the 3-way merge base (``synced.hash``) used to live in the tracked
frontmatter, so git and grison were *both* authoritative over it — a ``git checkout`` could
revert the base to a stale value and the next sync would "heal" it with a surprising pull.
Here the base and the evidence hash/meta/basename bookkeeping live under ``.grison/state/``
(already gitignored, like ``.grison/env``/``mirrors.json``), keyed by the record's *remote
identity*. The content file keeps only content + a stable identity anchor; git can't touch
what it never sees.

The in-memory :class:`~grison.model.Finding` stays rich — :func:`hydrate_finding` fills its
``synced``/evidence-gw fields from the store right after parse, and :func:`persist_finding`
writes them back on every stamp/finalize — so the reconcile engine keeps operating on one
object and only the read/write endpoints moved.

Layout (per-record JSON, atomic temp+rename, so a crash mid-batch never half-writes a file):

    .grison/state/finding/<id>.json
    .grison/state/reportedFinding/<id>.json
    .grison/state/page/<page_id>.json
    .grison/state/report/<report_id>.json
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from grison.model import Finding
from grison.model.finding import SyncState


class _S(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate old/extra keys, never crash a sync


class BaseState(_S):
    """The 3-way merge base — content hash + time of the last sync."""

    hash: str | None = None
    at: datetime | None = None


class EvidenceState(_S):
    """Per-image volatile bookkeeping (keyed by the evidence's GW id in FindingState).

    ``hash`` = image-bytes sha256 at last sync (same-name byte-drift guard); ``meta`` =
    caption/friendly_name/description 3-way base; ``basename`` = server storage filename
    (local-rename guard). All three ride *alongside* the evidence id, which stays in the
    file as identity."""

    hash: str | None = None
    meta: str | None = None
    basename: str | None = None


class FindingState(_S):
    """State store entry for one ``finding``/``reportedFinding`` record."""

    base: BaseState = Field(default_factory=BaseState)
    evidence: dict[str, EvidenceState] = Field(default_factory=dict)  # key = str(gw.id)


class PageState(_S):
    """State store entry for one BookStack methodology page."""

    base: BaseState = Field(default_factory=BaseState)
    remote_updated_at: str | None = None
    remote_revision_count: int | None = None
    book_id: int | None = None      # cached remote placement witness (drift/move detection)
    chapter_id: int | None = None


class ReportState(_S):
    """State store entry for one report's narrative-section merge bases."""

    sections: dict[str, str] = Field(default_factory=dict)      # key -> section hash
    removed_remotely: list[str] = Field(default_factory=list)   # keys gone remotely, kept local


class StateStore:
    """Per-record JSON store under ``.grison/state/``. Single-writer (the sync verb already
    holds the workspace flock), so plain read/atomic-write with no locking of its own."""

    def __init__(self, root: Path) -> None:
        self._dir = root / ".grison" / "state"

    def _path(self, kind: str, ident: int) -> Path:
        return self._dir / kind / f"{ident}.json"

    @staticmethod
    def _read(path: Path, model: type[_S]) -> _S | None:
        if not path.exists():
            return None
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # a corrupt state file must not abort the sync — treat as "no state" (the record
            # reclassifies as a broken link / re-pull, never a silent wrong merge).
            return None

    @staticmethod
    def _write(path: Path, model: _S) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(model.model_dump_json(exclude_none=True), encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX — no half-written state file

    # --- findings -------------------------------------------------------------
    def get_finding(self, table: str, ident: int) -> FindingState | None:
        return self._read(self._path(table, ident), FindingState)  # type: ignore[return-value]

    def put_finding(self, table: str, ident: int, state: FindingState) -> None:
        self._write(self._path(table, ident), state)

    def delete_finding(self, table: str, ident: int) -> None:
        self._path(table, ident).unlink(missing_ok=True)

    # --- methodology pages ----------------------------------------------------
    def get_page(self, page_id: int) -> PageState | None:
        return self._read(self._path("page", page_id), PageState)  # type: ignore[return-value]

    def put_page(self, page_id: int, state: PageState) -> None:
        self._write(self._path("page", page_id), state)

    def delete_page(self, page_id: int) -> None:
        self._path("page", page_id).unlink(missing_ok=True)

    # --- reports --------------------------------------------------------------
    def get_report(self, report_id: int) -> ReportState | None:
        return self._read(self._path("report", report_id), ReportState)  # type: ignore[return-value]

    def put_report(self, report_id: int, state: ReportState) -> None:
        self._write(self._path("report", report_id), state)


def hydrate_finding(store: StateStore, finding: Finding) -> Finding:
    """Fill a just-parsed finding's volatile fields (merge base + per-image
    hash/meta/basename) from the store, keyed by remote identity. A no-op when the record
    has no identity yet (never synced) or no stored state — the finding then reads as
    "no base", which the classifier already handles (insert / broken-link)."""
    gw = finding.grison.gw
    if gw.table is None or gw.id is None:
        return finding
    st = store.get_finding(gw.table, gw.id)
    if st is None:
        return finding
    if st.base.hash is not None:
        finding.grison.synced = SyncState(hash=st.base.hash, at=st.base.at)
    for e in finding.evidence:
        if e.gw is None or e.gw.id is None:
            continue
        es = st.evidence.get(str(e.gw.id))
        if es is not None:
            e.gw.hash, e.gw.meta, e.gw.basename = es.hash, es.meta, es.basename
    return finding


def persist_finding(store: StateStore, finding: Finding) -> None:
    """Write a finding's merge base + per-image bookkeeping to the store. Replaces the
    former "stamp it into the frontmatter" effect. No-op without identity (can't key it) —
    callers stamp identity (the new gw.id) *before* persisting, same order as before."""
    gw = finding.grison.gw
    if gw.table is None or gw.id is None:
        return
    st = FindingState()
    if finding.grison.synced is not None:
        st.base = BaseState(hash=finding.grison.synced.hash, at=finding.grison.synced.at)
    for e in finding.evidence:
        if e.gw is not None and e.gw.id is not None:
            st.evidence[str(e.gw.id)] = EvidenceState(
                hash=e.gw.hash, meta=e.gw.meta, basename=e.gw.basename
            )
    store.put_finding(gw.table, gw.id, st)
