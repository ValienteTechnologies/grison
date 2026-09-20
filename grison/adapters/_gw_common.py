"""Shared Ghostwriter lookups the findings-phase and reports-phase adapters need —
never imported by :mod:`grison.engine` itself (see ``tests/test_engine_no_leak.py``).

Two contexts live here because they scope differently: :class:`GWContext` is a bare
path→report-id lookup the findings/evidence adapters use to find which report a
document sits under; :class:`GWReportContext` is the richer per-run state (reports,
extra-field specs, operator id, directory maps in both directions) the report
narrative/notes adapters need. Both are built from the same live client + index.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from grison.adapters._slug import slugify as _slugify
from grison.index import Index, IndexKind
from grison.markdown.refs import LocalRef, RemoteRef
from grison.markdown.refscan import decode_ref_path
from grison.remote.ghostwriter import GhostwriterClient


def slugify(name: str) -> str:
    """A filesystem-safe, ``[a-z0-9][a-z0-9._-]*``-matching slug from a report title
    — used once, on pull, to name a brand-new report directory (D3/D4: file and
    directory names are stable handles, derived once and never renamed)."""
    return _slugify(name, fallback="report")


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
    _evidence_cache: list[dict[str, Any]] | None = field(default=None, repr=False, compare=False)

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
        self._evidence_cache = [r for r in self._evidence_cache if r.get("id") != evidence_id]


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
    #: The org-wide evidence-row cache backing :meth:`evidence_for_report` — same
    #: caching shape as :class:`GWContext`'s own :meth:`~GWContext.all_evidence`
    #: (Ghostwriter has no report-scoped evidence query); private, read only
    #: through that method so it's never read half-populated.
    _evidence_cache: list[dict[str, Any]] | None = field(default=None, repr=False, compare=False)

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

    def evidence_for_report(self, report_id: int) -> dict[int, dict[str, Any]]:
        """Every ``gw.evidence`` row belonging to ``report_id``, in the small shape
        :class:`IndexRefResolver` needs (``friendly_name``/``caption``/``description``)
        to carry a pulled embed's caption/description through on ``to_local``.
        Fetched org-wide at most once per sync run (Ghostwriter has no
        report-scoped evidence query) and cached on this context, filtered by
        report here."""
        if self._evidence_cache is None:
            self._evidence_cache = list(self.client.fetch_evidence())
        return {
            row["id"]: {
                "friendly_name": row.get("friendlyName") or "",
                "caption": row.get("caption") or "",
                "description": row.get("description") or "",
            }
            for row in self._evidence_cache
            if row.get("reportId") == report_id
        }

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
    report's own indexed ``gw.evidence`` entries. ``evidence_rows`` (id ->
    ``{friendly_name, caption, description}``, as :meth:`GWReportContext.
    evidence_for_report` returns) is OPTIONAL and empty by default — a caller that
    only needs id resolution (``to_remote``/``to_remote_id``, e.g. the embed-id
    folding :func:`grison.engine.filesets.canonical_prose` does — which never reads
    caption/description at all: a referencing document's own canonical payload
    excludes them, see that function's docstring) never has to supply it.
    ``to_local`` carries the evidence row's OWN caption/description through to the
    rendered ``![caption](path "description")`` line whenever ``evidence_rows`` is
    given (a pulled narrative section/note used to always lose a captioned embed's
    caption/description here — the bug this field exists to fix), AND resolves a
    cross-reference span by NAME when ``remote.id`` is ``None`` (a cross-
    reference's native HTML carries only its ``ref`` name, never an id — see
    :mod:`grison.markdown.converter`'s module docstring) — a pulled narrative
    section/note cross-reference used to always round-trip as the unresolved
    ``gw:evidence-ref:name=...`` placeholder here, even for an evidence row synced
    down long ago. This is the ONE resolver for every report-scoped text kind
    (narrative sections, notes, reported findings) — a near-identical resolver used
    to be hand-duplicated in :mod:`grison.adapters.gw_findings` as
    ``GwRefResolver``, and the same bugs above were fixed twice; that copy is gone.
    An unresolved reference is never fatal
    by itself (see the converter's own "unresolved references" handling), so
    this is a safe, honest floor to build on rather than a stub: PUSH of an
    unknown reference is a clear ``ConverterError`` (surfaced by the validator
    as ``REP-001``, per the rework's brief D1/D+ENGINE.md), and PULL of one
    round-trips as a visible placeholder instead of failing.
    """

    index: Index
    report_dir: str  # this report's directory name, workspace-relative under findings/reports/
    #: A plain dict, OR a zero-arg callable returning one (:meth:`_rows` calls it at
    #: most once, on first actual need) — a caller with a report that HAS evidence
    #: references passes ``lambda: ctx.evidence_for_report(report_id)`` so the
    #: (cached, but still real) org-wide evidence fetch it triggers only ever
    #: happens for a report whose narrative/notes actually embed something, never
    #: for one with none (see :mod:`grison.adapters.gw_report`'s ``_resolver_for``:
    #: without this, EVERY section of EVERY report would trigger the fetch merely
    #: by being scanned, breaking the "evidence fetched at most once per sync"
    #: invariant :mod:`grison.adapters.gw_evidence` already guarantees).
    evidence_rows: dict[int, dict[str, Any]] | Callable[[], dict[int, dict[str, Any]]] = field(
        default_factory=dict
    )

    def _rows(self) -> dict[int, dict[str, Any]]:
        return self.evidence_rows() if callable(self.evidence_rows) else self.evidence_rows

    def rows(self) -> dict[int, dict[str, Any]]:
        """Public alias of :meth:`_rows` — for a caller that needs the resolved
        ``{id: {friendly_name, caption, description}}`` mapping itself (e.g. to
        build its own name -> id lookup), not just single-id resolution."""
        return self._rows()

    def _id_for_local_path(self, path: str) -> int | None:
        if not path.startswith("evidence/"):
            return None
        # ``path`` reaches here either already-decoded (a scan_refs-sourced call,
        # e.g. grison.engine.filesets.canonical_prose) or still percent-encoded (a
        # call from grison.markdown.converter's OWN, separate markdown-it parse,
        # during an actual push) — decode_ref_path is a safe no-op on the former.
        full = f"findings/reports/{self.report_dir}/{decode_ref_path(path)}"
        rec = self.index.get(full)
        if rec is None or rec.kind is not IndexKind.GW_EVIDENCE:
            return None
        return rec.id

    def to_remote(self, path: str) -> RemoteRef | None:
        eid = self._id_for_local_path(path)
        if eid is None:
            return None
        row = self._rows().get(eid, {})
        name = row.get("friendly_name") or path[len("evidence/") :].rsplit(".", 1)[0]
        return RemoteRef("gw-evidence", id=eid, name=name, url=None)

    def to_remote_id(self, path: str) -> int | None:
        """:class:`~grison.engine.filesets.ResolvesEmbeds`'s one method — the seam
        :func:`~grison.engine.filesets.canonical_prose` uses to fold each embed's
        CURRENT remote id into a referencing document's canonical payload, so bytes
        changing under an unchanged path (a reupload: new remote row, old one
        deleted — see that module's docstring) changes the document's hash even
        though its own markdown text (which only ever names the stable path) does
        not, and the document gets re-pushed with the new id instead of silently
        staying CLEAN or PULLing an unresolved-reference placeholder over it."""
        return self._id_for_local_path(path)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        if remote.kind != "gw-evidence":
            return None
        eid = remote.id
        if eid is None and remote.name is not None:
            # A cross-reference span (D1) carries no id at all — only its
            # ``ref`` name (see grison.markdown.converter's module docstring) —
            # so ``remote.id`` is ALWAYS ``None`` for one; without this fallback
            # this resolver could never resolve a cross-reference at all (every
            # narrative section/note/reported-finding cross-reference round-
            # tripped as the unresolved `` `gw:evidence-ref:name=...` ``
            # placeholder here, even for an evidence row synced down long ago).
            eid = next(
                (i for i, r in self._rows().items() if r.get("friendly_name") == remote.name),
                None,
            )
        if eid is None:
            return None
        full = self.index.path_of(IndexKind.GW_EVIDENCE, eid)
        prefix = f"findings/reports/{self.report_dir}/"
        if full is None or not full.startswith(prefix):
            return None
        row = self._rows().get(eid, {})
        return LocalRef(
            path=full[len(prefix) :],
            caption=row.get("caption", ""),
            description=row.get("description", ""),
        )


__all__: list[str] = [
    "GWContext",
    "GWReportContext",
    "IndexRefResolver",
    "build_context",
    "slugify",
]
