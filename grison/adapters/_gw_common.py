"""Shared Ghostwriter lookups the findings-phase and reports-phase adapters need —
never imported by :mod:`grison.engine` itself (see ``tests/test_engine_no_leak.py``).

Two contexts live here because they scope differently: :class:`GWContext` is a bare
path→report-id lookup the findings/evidence adapters use to find which report a
document sits under; :class:`GWReportContext` is the richer per-run state (reports,
extra-field specs, operator id, directory maps in both directions) the report
narrative/notes adapters need. Both are built from the same live client + index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from grison.index import Index, IndexKind
from grison.markdown.refs import LocalRef, RemoteRef
from grison.remote.ghostwriter import GhostwriterClient

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A filesystem-safe, ``[a-z0-9][a-z0-9._-]*``-matching slug from a report title
    — used once, on pull, to name a brand-new report directory (D3/D4: file and
    directory names are stable handles, derived once and never renamed)."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "report"


@dataclass
class GWContext:
    """Everything the findings-phase adapters share for one sync run: the live
    client, plus every ``gw.report`` directory currently indexed — a report
    directory's identity comes ONLY from its index entry (D3/BRIEF workspace format
    §1.3), never from parsing an id out of its name."""

    client: GhostwriterClient
    report_dirs: dict[PurePosixPath, int] = field(default_factory=dict)
    #: The org-wide evidence-row cache (:meth:`all_evidence`) — private: every
    #: reader/writer goes through the methods below so the cache is never read
    #: half-populated or mutated inconsistently with what the server actually holds.
    _evidence_cache: list[dict[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def build(cls, client: GhostwriterClient, index: Index) -> GWContext:
        report_dirs = {
            PurePosixPath(p): rec.id
            for p, rec in index.records.items()
            if rec.kind is IndexKind.GW_REPORT
        }
        return cls(client=client, report_dirs=report_dirs)

    def report_id_for(self, path: PurePosixPath) -> int | None:
        """The report id owning ``path`` (a file or directory anywhere under a
        report directory) — walks up to the nearest indexed ``gw.report`` ancestor."""
        for ancestor in (path, *path.parents):
            rid = self.report_dirs.get(ancestor)
            if rid is not None:
                return rid
        return None

    def all_evidence(self) -> list[dict[str, Any]]:
        """Every evidence row across the whole org — Ghostwriter has no
        report-scoped evidence query, so :class:`~grison.adapters.gw_evidence.
        GwEvidenceAdapter` used to pay for this org-wide fetch once per report's
        ``list_remote`` call AND once per uploaded file's friendly-name de-dup
        check. Fetched at most once per sync run and cached here; kept accurate
        through the run by :meth:`evidence_cache_upsert`/:meth:`evidence_cache_remove`,
        which the adapter calls as it creates/updates/deletes rows, so within-run
        de-dup still sees names uploaded a moment ago without a second fetch."""
        if self._evidence_cache is None:
            self._evidence_cache = list(self.client.fetch_evidence())
        return self._evidence_cache

    def evidence_cache_upsert(self, row: dict[str, Any]) -> None:
        """Record a just-created/updated evidence row locally. A no-op before the
        cache exists (before the first :meth:`all_evidence` call) — the eventual
        real fetch will already include it, so there is nothing to reconcile."""
        if self._evidence_cache is None:
            return
        for i, existing in enumerate(self._evidence_cache):
            if existing.get("id") == row.get("id"):
                self._evidence_cache[i] = row
                return
        self._evidence_cache.append(row)

    def evidence_cache_remove(self, evidence_id: int) -> None:
        """The delete-side counterpart of :meth:`evidence_cache_upsert`."""
        if self._evidence_cache is None:
            return
        self._evidence_cache = [
            r for r in self._evidence_cache if r.get("id") != evidence_id
        ]


@dataclass
class GWReportContext:
    """Everything the reports/notes adapters need for one sync run.

    ``dir_by_report_id``/``report_id_by_dir`` are populated by the CLI from
    ``.grison/index.json`` (kind ``gw.report``) AFTER :func:`grison.adapters.
    gw_report.sync_report_dirs` has created any brand-new report directory this
    run — so every report a narrative/note record could possibly belong to already
    has a resolvable directory by the time :mod:`grison.engine.apply` runs.
    """

    client: GhostwriterClient
    index: Index
    reports: list[dict[str, Any]] = field(default_factory=list)
    field_specs: list[dict[str, Any]] = field(default_factory=list)
    dir_by_report_id: dict[int, str] = field(default_factory=dict)
    report_id_by_dir: dict[str, int] = field(default_factory=dict)
    _operator: tuple[int, str] | None = None

    @property
    def reports_by_id(self) -> dict[int, dict[str, Any]]:
        return {r["id"]: r for r in self.reports}

    @property
    def project_id_by_report(self) -> dict[int, int | None]:
        return {r["id"]: (r.get("project") or {}).get("id") for r in self.reports}

    def refresh(self) -> None:
        self.reports = self.client.fetch_reports()
        self.field_specs = sorted(
            self.client.fetch_report_extra_field_specs(),
            key=lambda s: (s.get("position") is None, s.get("position"), s["id"]),
        )

    def resolve_operator(self) -> tuple[int, str]:
        """Resolve+cache the syncing operator's GW user id (whoami -> user lookup)
        once per sync — ``insert_project_note`` has no session-derived
        ``operatorId``, so it must be supplied on every note push."""
        if self._operator is None:
            username = self.client.whoami()["username"]
            user_id = self.client.resolve_user_id(username)
            if user_id is None:
                raise RuntimeError(f"could not resolve Ghostwriter user id for {username!r}")
            self._operator = (user_id, username)
        return self._operator


def build_context(client: GhostwriterClient, index: Index) -> GWReportContext:
    ctx = GWReportContext(client=client, index=index)
    ctx.refresh()
    return ctx


@dataclass(frozen=True)
class IndexRefResolver:
    """The push/pull :class:`~grison.markdown.refs.RefResolver` for report-scoped
    text (narrative sections, notes): resolves ``evidence/<file>`` against this
    report's own indexed ``gw.evidence`` entries. Deliberately minimal — it has no
    caption/description (those live on the evidence row itself, which
    :mod:`grison.adapters.gw_evidence` owns); an unresolved reference is never fatal
    by itself (see the converter's own "unresolved references" handling), so this is
    a safe, honest floor to build on rather than a stub: PUSH of an unknown reference
    is a clear ``ConverterError`` (surfaced by the validator as ``REP-001``, per the
    rework's brief D1/D+ENGINE.md), and PULL of one round-trips as a visible
    placeholder instead of failing.
    """

    index: Index
    report_dir: str  # this report's directory name, workspace-relative under findings/reports/

    def to_remote(self, path: str) -> RemoteRef | None:
        if not path.startswith("evidence/"):
            return None
        full = f"findings/reports/{self.report_dir}/{path}"
        rec = self.index.get(full)
        if rec is None or rec.kind is not IndexKind.GW_EVIDENCE:
            return None
        name = path[len("evidence/") :].rsplit(".", 1)[0]
        return RemoteRef("gw-evidence", id=rec.id, name=name, url=None)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        if remote.kind != "gw-evidence" or remote.id is None:
            return None
        full = self.index.path_of(IndexKind.GW_EVIDENCE, remote.id)
        prefix = f"findings/reports/{self.report_dir}/"
        if full is None or not full.startswith(prefix):
            return None
        return LocalRef(path=full[len(prefix) :])


__all__: list[str] = [
    "GWContext",
    "GWReportContext",
    "IndexRefResolver",
    "build_context",
    "slugify",
]
