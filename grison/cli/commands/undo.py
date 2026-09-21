"""``grison undo`` — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import typer

from grison.adapters import bs_structure
from grison.adapters._bs_common import build_context
from grison.adapters._gw_common import GWContext
from grison.adapters._gw_common import build_context as build_gw_context
from grison.adapters.bs_images import BsImagesAdapter
from grison.adapters.bs_pages import BsPageAdapter
from grison.adapters.gw_evidence import GwEvidenceAdapter
from grison.adapters.gw_findings import GwLibraryFindingAdapter, GwReportedFindingAdapter
from grison.adapters.gw_notes import ReportNoteAdapter
from grison.adapters.gw_report import NarrativeSectionAdapter
from grison.cli import app, clients
from grison.cli.guards import _guarded, _refuse_if_format_mismatch, _refuse_if_workspace_rules_fail
from grison.cli.phases.reports import _refresh_report_dirs
from grison.engine.adapter import CreateUndoAdapter
from grison.engine.model import RemoteRecord
from grison.engine.state import StateStore
from grison.engine.undo import describe_snapshot as engine_describe_snapshot
from grison.engine.undo import list_snapshots as engine_list_snapshots
from grison.engine.undo import replay as engine_undo_replay
from grison.engine.undo import snapshot_kinds as engine_snapshot_kinds
from grison.index import Index
from grison.remote.creds import load as load_creds
from grison.validator import find_workspace_root


@dataclass
class _BoundAdapter:
    """Ignores whatever ``ctx`` :func:`grison.engine.undo.replay` passes in and
    always uses its own closed-over remote context instead — ``replay`` calls
    every adapter with the SAME ``ctx`` object regardless of kind, but a
    Ghostwriter adapter's ``ctx.client`` and a BookStack adapter's ``ctx.client``
    are different client types; this wrapper is how ``undo`` lets both remotes'
    adapters share one snapshot replay without forcing one shared ctx shape onto
    both. :func:`grison.engine.undo.replay` calls ``refetch``/``delete``/``restore``
    on every undo-time adapter, plus ``render_local`` (no ``ctx`` needed — it is a
    pure formatter) when a DELETE_REMOTE undo must also restore the local mirror
    file, and ``canonical_remote`` (also no ``ctx``) for a push/move_edit undo's
    drift check (item 4 — :class:`~grison.engine.adapter.PushUndoAdapter`) —
    never ``create``/``update``/``fetch_remote``/etc."""

    inner: Any
    bound_ctx: Any
    kind: str = field(init=False)

    def __post_init__(self) -> None:
        # a plain settable attribute, not a read-only property: CreateUndoAdapter's
        # `kind: str` protocol member is a settable variable, and a `dict[str,
        # CreateUndoAdapter]` value must match that structurally.
        self.kind = str(self.inner.kind)

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None:
        del ctx
        return self.inner.refetch(self.bound_ctx, id)

    def delete(self, ctx: Any, id: int) -> None:
        del ctx
        self.inner.delete(self.bound_ctx, id)

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord:
        del ctx
        return self.inner.restore(self.bound_ctx, preimage)

    def canonical_remote(self, data: Any) -> Any:
        return self.inner.canonical_remote(data)

    def render_local(self, data: Any, *, path: PurePosixPath) -> str:
        render = self.inner.render_local
        return str(render(data, path=path))


@app.command(help="Reverse the last sync's remote writes.")
@_guarded
def undo(
    snapshot: Annotated[
        str | None,
        typer.Argument(help="Snapshot name (default: the most recent one)."),
    ] = None,
    list_: Annotated[
        bool,
        typer.Option("--list", help="List available snapshots, newest first, and exit."),
    ] = False,
) -> None:
    """Dev notes (user-facing behavior is in ``--help``/README's ``## Commands``):

    A snapshot may hold both Ghostwriter and BookStack kinds together (one
    snapshot per sync RUN, not per phase); which remote(s) to contact, and which
    adapters to build, is decided by looking at the kinds the snapshot actually
    recorded — never touching a remote the snapshot doesn't need.
    """
    root = find_workspace_root(Path.cwd())
    _refuse_if_format_mismatch(root)  # D13 (item 10) — before any work
    _refuse_if_workspace_rules_fail(root)  # item 1 — before any replay
    names = engine_list_snapshots(root)
    if list_:
        if not names:
            typer.echo("no snapshots")
        for n in names:
            typer.echo(engine_describe_snapshot(root, n).render())
        return
    if not names:
        typer.secho("no snapshots to undo", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    target = snapshot or names[0]
    if target not in names:
        typer.secho(f"error: no such snapshot {target!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    kinds = engine_snapshot_kinds(root, target)
    findings_gw_kinds = {
        GwLibraryFindingAdapter.kind,
        GwReportedFindingAdapter.kind,
        GwEvidenceAdapter.kind,
    }
    report_gw_kinds = {NarrativeSectionAdapter.kind, ReportNoteAdapter.kind}
    bs_kinds = {
        BsPageAdapter.kind,
        bs_structure.BookUndoAdapter.kind,
        bs_structure.ChapterUndoAdapter.kind,
        BsImagesAdapter.kind,
    }
    creds = load_creds(root)
    if not (kinds & (findings_gw_kinds | report_gw_kinds)) and not (kinds & bs_kinds):
        typer.secho(
            f"error: snapshot {target!r} has no known record kinds to undo",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    state = StateStore(root)
    index = Index.load(root)
    with contextlib.ExitStack() as stack:
        # A single snapshot's ops may come from any mix of the run's phases (findings,
        # report, wiki — `grison sync` shares ONE Snapshot across all of them, see
        # its own docstring), but `undo.replay` takes one `ctx` for every adapter it
        # calls regardless of kind, and a Ghostwriter
        # adapter's `ctx.client` and a BookStack adapter's `ctx.client` are
        # different types (and even within Ghostwriter, the findings adapters and
        # the report/note adapters take different context shapes — see
        # `_gw_common`) — so every adapter below is a thin wrapper that closes
        # over its OWN remote context and ignores whatever `ctx` `replay` happens
        # to pass it, rather than forcing one shared ctx shape onto everything.
        # Only the remote(s) this snapshot's kinds actually touch are contacted.
        adapters: dict[str, CreateUndoAdapter] = {}
        if kinds & (findings_gw_kinds | report_gw_kinds):
            creds.require_ghostwriter()
            gw_client = stack.enter_context(clients._make_gw_client(creds))
            if kinds & findings_gw_kinds:
                gw_ctx = GWContext.build(gw_client, index)
                # item 3 (fix-undo-repair): the SAME evidence_by_report a real sync's
                # findings phase builds (_run_reports_phase/_run_findings_phase) — a
                # reported finding's own canonical_remote() resolves a plain
                # cross-reference (`[caption](evidence/x.png)`, by NAME on the wire —
                # see grison.engine.filesets._LiteralEmbedResolver.to_local) through
                # these rows; an EMPTY dict here (the bug) makes undo's own restamp
                # resolve that name to a placeholder instead of the real evidence id,
                # so it can never agree with what the next real sync computes — either
                # a spurious "changed since the sync, not restoring" refusal (this
                # op's own pre-write re-fetch guard) or a one-off benign `repair` on
                # the sync right after undo, depending on which of the two
                # canonical_remote() calls it trips. An embed (native
                # `data-evidence-id="N"`) resolves by id via the index alone and was
                # never affected — only cross-references were.
                evidence_by_report: dict[int, dict[int, dict[str, Any]]] = {}
                for report_id in gw_ctx.report_dirs.values():
                    rows = GwEvidenceAdapter(report_id=report_id).list_remote(gw_ctx)
                    evidence_by_report[report_id] = {i: r.data for i, r in rows.items()}
                # the library adapter's ctx is the bare client (no report scoping
                # — see its class docstring), the other two take the GWContext.
                adapters[GwLibraryFindingAdapter.kind] = _BoundAdapter(
                    GwLibraryFindingAdapter(),
                    gw_client,
                )
                adapters[GwReportedFindingAdapter.kind] = _BoundAdapter(
                    GwReportedFindingAdapter(index=index, evidence_by_report=evidence_by_report),
                    gw_ctx,
                )
                adapters[GwEvidenceAdapter.kind] = _BoundAdapter(
                    GwEvidenceAdapter(report_id=0),
                    gw_ctx,
                )
            if kinds & report_gw_kinds:
                gw_report_ctx = build_gw_context(gw_client, index)
                _refresh_report_dirs(gw_report_ctx, index)
                adapters[NarrativeSectionAdapter.kind] = _BoundAdapter(
                    NarrativeSectionAdapter(),
                    gw_report_ctx,
                )
                adapters[ReportNoteAdapter.kind] = _BoundAdapter(
                    ReportNoteAdapter(),
                    gw_report_ctx,
                )
        if kinds & bs_kinds:
            creds.require_bookstack()
            bs_client = stack.enter_context(clients._make_bs_client(creds))
            bs_ctx = build_context(bs_client, state)
            all_page_ids = frozenset(p["id"] for p in bs_client.fetch_pages())
            adapters[BsPageAdapter.kind] = _BoundAdapter(BsPageAdapter(), bs_ctx)
            adapters[bs_structure.BookUndoAdapter.kind] = bs_structure.BookUndoAdapter(bs_client)
            adapters[bs_structure.ChapterUndoAdapter.kind] = bs_structure.ChapterUndoAdapter(
                bs_client
            )
            adapters[BsImagesAdapter.kind] = _BoundAdapter(
                BsImagesAdapter(book_id=0, page_ids=all_page_ids, anchor_page_id=None),
                bs_ctx,
            )
        problems = engine_undo_replay(
            root,
            target,
            ctx=None,
            adapters=adapters,
            on_event=lambda msg: typer.secho(msg, dim=True),
        )
    for p in problems:
        typer.secho(f"  ! {p}", fg=typer.colors.RED)
    if problems:
        raise typer.Exit(code=1)
