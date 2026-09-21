"""Shared plumbing for the three sync-engine phase runners (findings, reports,
wiki) — see :mod:`grison.cli` for the CLI itself.

:func:`run_phase`/:class:`PhaseSpec` absorb what the three ``_run_*_phase``
functions in :mod:`grison.cli.phases.findings`/``reports``/``wiki`` repeat:
``Index.load``/``StateStore`` construction, the scoped ``validate_workspace`` +
prefix filter, ``RunOptions``/``FilesetRunOptions`` construction, the engine-run
loop (events/summaries/plans), the fileset loop (the ``<kind>[<dir>]`` summary
key, plus per-record try/except isolation), and the ``index.save()`` epilogue.
Each phase still owns its own result dataclass and its own thin ``_run_*_phase``
wrapper (the seam tests monkeypatch) — this module supplies their shared
internals only, through the hooks on :class:`PhaseSpec`.

:func:`_run_phase` (the OTHER function here, singular-underscore) is unrelated:
it is ``grison.cli.commands.sync``'s per-phase ISOLATION wrapper — catches an
exception from a whole phase call so the phases after it still run — not part of
the :class:`PhaseSpec` machinery.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import typer

from grison.engine.apply import RunOptions
from grison.engine.apply import run as engine_run
from grison.engine.filesets import RunOptions as FilesetRunOptions
from grison.engine.filesets import sync_fileset as engine_sync_fileset
from grison.engine.model import Event, KindSummary, Outcome, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.index import Index
from grison.validator import validate_workspace
from grison.validator.registry import Failure

_T = TypeVar("_T")


def _run_phase(name: str, fn: Callable[[], _T], phase_errors: list[str]) -> _T | None:
    """Run one sync phase (findings / reports / wiki) in isolation. An exception
    here is recorded in ``phase_errors`` and printed, but never propagated — the
    phases that follow still get to run, and the run exits nonzero at the end
    regardless."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — isolate this phase, subsequent phases still run
        msg = f"{name} sync failed: {e}"
        phase_errors.append(msg)
        typer.secho(msg, fg=typer.colors.RED)
        return None


def relative_force_set(
    root: Path, paths: set[Path], prefix: tuple[str, ...]
) -> frozenset[PurePosixPath]:
    """A ``--force-local``/``--force-remote`` path set, made workspace-relative and
    scoped to this phase's own subtree (``prefix`` — ``("findings",)``,
    ``("methodology",)``, or ``("findings", "reports")``). A path outside ``root``,
    or outside ``prefix``, is silently dropped: each phase only ever forces paths
    inside its own scope, so the SAME caller-supplied path set is filtered
    independently per phase, once per phase, each with its own ``prefix``."""
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts[: len(prefix)] == prefix:
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


def scoped_failures(root: Path, subtree: str) -> list[Failure]:
    """``validate_workspace(root, paths=[root / <subtree>])``, filtered down to
    failures whose own ``path`` is inside ``subtree`` (a ``str.startswith`` check —
    ``subtree`` is e.g. ``"findings"``, ``"methodology"``, ``"findings/reports"``).
    :func:`run_phase` calls this once, up front; the reports phase's own
    ``structure`` hook calls it again, AFTER ``sync_report_dirs`` has (possibly)
    created/materialized a report directory this same run — see
    :mod:`grison.cli.phases.reports`'s own docstring for why that phase alone
    re-validates after its structure pass instead of before it."""
    subtree_path = root.joinpath(*subtree.split("/"))
    all_failures = validate_workspace(root, paths=[subtree_path])
    return [f for f in all_failures if f.path.startswith(subtree)]


# One entry per file set a phase syncs — e.g. one per report's evidence/
# (reports) or one per book's images/ (wiki): (label_dir, folder, ctx, adapter,
# doc_bodies). ``label_dir`` (the report/book directory) names the summary-dict
# key (``f"{adapter.kind}[{label_dir}]"``, e.g. ``gw.evidence[findings/reports/
# report-a]``/``bs.image[methodology/library/book-a]``) — distinct from
# ``folder`` (``label_dir / "evidence"``/``label_dir / "images"``, what actually
# gets synced and what a failed item's own Plan/Event ``path`` names), matching
# the two call sites' own pre-split convention.
FilesetItem = tuple[PurePosixPath, PurePosixPath, Any, Any, "dict[PurePosixPath, str] | None"]


@dataclass
class PhaseCtx:
    """What every :class:`PhaseSpec` hook receives — the plumbing common to every
    phase, plus ``failures`` (mutable: a hook may re-validate and replace it, see
    :func:`scoped_failures`'s docstring) and ``extra``, a free-form bag hooks use to
    pass their own phase-specific state to a LATER hook — the remote ctx(s) a
    ``build_ctx`` hook builds, for ``fileset_items``/``engine_steps`` to read back;
    the reports phase's own ``evidence_by_report``, stashed by its ``post_fileset``
    hook for its narrative-section engine step to read."""

    root: Path
    client: Any
    index: Index
    state: StateStore
    snapshot: Snapshot
    dry_run: bool
    quiet: bool
    failures: list[Failure]
    options: RunOptions
    fs_options: FilesetRunOptions
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseResult:
    """:func:`run_phase`'s own return shape — plans/events/summaries across every
    engine/fileset step the phase ran, plus whatever phase-specific extras its
    hooks stashed in ``PhaseCtx.extra`` (e.g. the wiki phase's own
    ``StructureResult``, the reports phase's ``ReportDirResult``). Each phase's own
    ``_run_*_phase`` wrapper unpacks this into its own result dataclass
    (``FindingsPhaseResult``/``ReportsPhaseResult``/``WikiPhaseResult``) — those
    keep their own names, fields and ``exit_code`` rules (read by
    ``grison.cli.payloads``/``render`` and monkeypatched directly by tests); this
    is only the generic container ``run_phase`` hands back."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseSpec:
    """One phase's shape, as data. Every hook below takes the SAME ``PhaseCtx`` and
    reads/writes ``PhaseCtx.extra`` (and, for ``structure``, may replace
    ``PhaseCtx.failures``) to pass its own state to a later hook, rather than
    ``run_phase`` threading a growing pile of phase-specific positional results
    between them. Hooks run in this fixed order: ``build_ctx``, ``structure``, the
    ``fileset_items`` loop (each success calling ``fileset_on_success``),
    ``post_fileset``, then the ``engine_steps`` loop."""

    name: str
    #: This phase's own subtree, as :func:`scoped_failures` wants it — e.g.
    #: ``"findings"``, ``"methodology"``, ``"findings/reports"``.
    validate_subtree: str
    #: This phase's own :func:`relative_force_set` prefix.
    force_prefix: tuple[str, ...]
    #: Builds this phase's own remote ctx(es) into ``PhaseCtx.extra`` (e.g.
    #: ``extra["ctx"]``) — called once, before everything else.
    build_ctx: Callable[[PhaseCtx], None]
    #: The book/chapter/shelf or report-directory structure pass (optional) — runs
    #: BEFORE the fileset loop, so a directory/book it creates is visible to the
    #: file sets that sync next.
    structure: Callable[[PhaseCtx], None] | None = None
    #: The file sets this phase syncs, evaluated once (after ``structure`` has
    #: possibly created new directories/books), one (folder, ctx, adapter,
    #: doc_bodies) tuple per file set.
    fileset_items: Callable[[PhaseCtx], Iterable[FilesetItem]] | None = None
    #: Runs after one fileset item's OWN sync succeeds — never for one that
    #: raised (e.g. the reports phase's caption-rewrite pass).
    fileset_on_success: (
        Callable[[PhaseCtx, PurePosixPath, dict[PurePosixPath, str] | None, Any], None] | None
    ) = None
    #: Runs once, after the whole fileset loop (e.g. the reports phase building
    #: ``evidence_by_report`` for the narrative/notes engine steps that follow).
    post_fileset: Callable[[PhaseCtx], None] | None = None
    #: The ``grison.engine.apply`` adapters this phase runs, one (ctx, adapter)
    #: pair per kind, run through ``grison.engine.apply.run`` in order.
    engine_steps: Callable[[PhaseCtx], Iterable[tuple[Any, Any]]] = lambda _pctx: ()


def run_phase(
    root: Path,
    client: Any,
    spec: PhaseSpec,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    allow_mass_change: bool = False,
    snapshot: Snapshot,
    quiet: bool = False,
) -> PhaseResult:
    """Run one sync phase from ``spec`` — see :class:`PhaseSpec`'s own docstring for
    what each hook does and the fixed order they run in."""
    index = Index.load(root)
    state = StateStore(root)

    fl = relative_force_set(root, force_local, spec.force_prefix)
    fr = relative_force_set(root, force_remote, spec.force_prefix)
    options = RunOptions(
        dry_run=dry_run, force_local=fl, force_remote=fr, allow_mass_change=allow_mass_change
    )
    fs_options = FilesetRunOptions(
        dry_run=dry_run, force_local=fl, force_remote=fr, allow_mass_change=allow_mass_change
    )

    pctx = PhaseCtx(
        root=root,
        client=client,
        index=index,
        state=state,
        snapshot=snapshot,
        dry_run=dry_run,
        quiet=quiet,
        failures=scoped_failures(root, spec.validate_subtree),
        options=options,
        fs_options=fs_options,
    )
    spec.build_ctx(pctx)
    if spec.structure is not None:
        spec.structure(pctx)

    plans: list[Plan] = []
    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}

    if spec.fileset_items is not None:
        for label_dir, folder, item_ctx, adapter, doc_bodies in spec.fileset_items(pctx):
            try:
                fs_result = engine_sync_fileset(
                    root,
                    item_ctx,
                    adapter,
                    folder,
                    index=index,
                    state=state,
                    snapshot=snapshot,
                    doc_bodies=doc_bodies,
                    options=fs_options,
                    failures=pctx.failures,
                )
            except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md
                # §5): one file set blowing up must not abort every other file
                # set/kind in this phase, any more than one record's own apply
                # step does inside grison.engine.filesets/apply themselves.
                reason = f"{type(e).__name__}: {e}"
                events.append(Event(verb="failed", path=str(folder), detail=reason))
                plans.append(
                    Plan(kind=adapter.kind, outcome=Outcome.FAILED, path=folder, reason=reason)
                )
                summaries[f"{adapter.kind}[{label_dir}]"] = KindSummary(
                    kind=adapter.kind, counts={"failed": 1}, problem_paths=[str(folder)]
                )
                continue
            plans.extend(fs_result.plans)
            events.extend(fs_result.events)
            summaries[f"{adapter.kind}[{label_dir}]"] = fs_result.summary
            if spec.fileset_on_success is not None:
                spec.fileset_on_success(pctx, label_dir, doc_bodies, fs_result)

    if spec.post_fileset is not None:
        spec.post_fileset(pctx)

    for item_ctx, adapter in spec.engine_steps(pctx):
        p, ev, s = engine_run(
            root,
            item_ctx,
            adapter,
            index=index,
            state=state,
            snapshot=snapshot,
            failures=pctx.failures,
            options=options,
        )
        plans.extend(p)
        events.extend(ev)
        summaries[adapter.kind] = s

    if not dry_run:
        index.save()

    return PhaseResult(plans=plans, events=events, summaries=summaries, extra=pctx.extra)
