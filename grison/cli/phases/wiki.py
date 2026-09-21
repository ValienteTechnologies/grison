"""The wiki (BookStack/methodology) sync phase — see :mod:`grison.cli` for the CLI
itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import typer

from grison.adapters import bs_structure
from grison.adapters._bs_common import build_context
from grison.adapters.bs_images import BsImagesAdapter, page_ids_in_book
from grison.adapters.bs_pages import BsPageAdapter
from grison.cli.phases.common import FilesetItem, PhaseCtx, PhaseSpec, run_phase
from grison.engine.model import Event, KindSummary, Plan
from grison.engine.undo import Snapshot
from grison.index import Index, IndexKind
from grison.markdown.refscan import scan_refs
from grison.remote.bookstack import BookStackClient


@dataclass
class WikiPhaseResult:
    """The wiki phase's result: every :class:`~grison.engine.model.Plan` reached (one
    per adapter — today just ``bs.page``, ENGINE.md's book/chapter/shelf structure
    step runs its own simpler pass, see :mod:`grison.adapters.bs_structure`), the
    events emitted, the per-kind summary, and the structure pass's own result."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    structure: bs_structure.StructureResult = field(default_factory=bs_structure.StructureResult)
    snapshot_dir: Path | None = None

    @property
    def exit_code(self) -> int:
        if any(p.is_problem for p in self.plans) or self.structure.errors:
            return 1
        return 0


def _run_wiki_phase(
    root: Path,
    client: BookStackClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    allow_mass_change: bool = False,
    snapshot: Snapshot,
    quiet: bool = False,
) -> WikiPhaseResult:
    """The wiki phase: BookStack structure (books/chapters/shelves — creates any new
    local book/chapter directory, then regenerates the read-only mirrors), then a
    book's ``images/`` file set (D9/BRIEF task C: BEFORE its pages, so a fresh
    upload's gallery URL is visible to the page push that follows — same ordering
    reason as gw.evidence-before-gw.reportSection, see the reports phase), then
    pages through :mod:`grison.engine.documents`. The validation gate is scoped to
    ``methodology/`` here (task step 1's scope parameter), same pattern as the
    findings phase's own ``findings/`` scoping — each phase validates only its own
    subtree; pulls are never blocked by it, only pushes/creates/deletes (see
    ``grison.engine.documents``'s own gate). Unlike the reports phase, validation here
    runs BEFORE the structure pass, not after — this phase's own ``structure`` hook
    never re-validates.

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot`, shared with
    the report and findings phases that already ran — this phase's own writes
    (structure, images, pages) append to it too, never their own snapshot; ``sync``
    persists it once, after this (the last) phase runs.

    ``quiet`` (item 8, fix-fin1): the structure pass's own progress lines
    ("create book …"/"mirror …") print unconditionally via ``typer.secho``
    regardless of ``--json`` — under ``sync --json``/``status --remote --json``
    that stray plain text would land BEFORE the one JSON document those commands
    print, breaking a consumer's ``json.loads`` on the combined output. ``quiet``
    (the caller's own ``json_output``) suppresses it; the same information is
    already in ``structure.created_books``/``materialized``/etc., which the
    JSON payload surfaces properly."""

    def build_ctx(pctx: PhaseCtx) -> None:
        ctx = build_context(client, pctx.state)
        ctx.indexed_page_ids = frozenset(
            rec.id for rec in pctx.index.records.values() if rec.kind.value == BsPageAdapter.kind
        )
        pctx.extra["ctx"] = ctx

    def structure(pctx: PhaseCtx) -> None:
        pctx.extra["structure"] = bs_structure.sync_structure(
            pctx.root,
            pctx.extra["ctx"],
            pctx.index,
            pctx.state,
            pctx.snapshot,
            dry_run=pctx.dry_run,
            on_event=None if pctx.quiet else lambda msg: typer.secho(msg, dim=True),
        )

    def fileset_items(pctx: PhaseCtx) -> list[FilesetItem]:
        ctx = pctx.extra["ctx"]
        items: list[FilesetItem] = []
        for book_dir, book_id in sorted(_book_dirs(pctx.index).items()):
            page_ids = page_ids_in_book(client, ctx, book_id)
            book_pages = [p for p in ctx.client.fetch_pages() if p["id"] in page_ids]
            anchor_page_id = min((p["id"] for p in book_pages), default=None)
            adapter = BsImagesAdapter(
                book_id=book_id,
                page_ids=page_ids,
                anchor_page_id=anchor_page_id,
                anchor_for=_anchor_for_book(pctx.root, pctx.index, book_dir),
            )
            items.append((book_dir, book_dir / "images", ctx, adapter, None))
        return items

    def engine_steps(pctx: PhaseCtx) -> list[tuple[Any, Any]]:
        return [(pctx.extra["ctx"], BsPageAdapter())]

    spec = PhaseSpec(
        name="wiki",
        validate_subtree="methodology",
        force_prefix=("methodology",),
        build_ctx=build_ctx,
        structure=structure,
        fileset_items=fileset_items,
        engine_steps=engine_steps,
    )
    result = run_phase(
        root,
        client,
        spec,
        dry_run=dry_run,
        force_local=force_local,
        force_remote=force_remote,
        allow_mass_change=allow_mass_change,
        snapshot=snapshot,
        quiet=quiet,
    )
    return WikiPhaseResult(
        plans=result.plans,
        events=result.events,
        summaries=result.summaries,
        structure=result.extra["structure"],
    )


def _book_dirs(index: Index) -> dict[PurePosixPath, int]:
    return {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind is IndexKind.BS_BOOK
    }


def _anchor_for_book(root: Path, index: Index, book_dir: PurePosixPath) -> dict[str, int]:
    """BRIEF C: a new gallery upload anchors to the page that already embeds it, not
    always the book's first page — :class:`~grison.adapters.bs_images.BsImagesAdapter`
    only falls back to ``anchor_page_id`` for a file no page references yet. Scans
    every ALREADY-INDEXED page's on-disk body (this runs before the pages phase, so
    a page this same sync is about to create has no id yet to anchor with — the
    first-page fallback covers that case too) with the token-based
    :mod:`grison.markdown.refscan`, never a regex, for an embed resolving to
    ``images/<file>`` (book root) or ``../images/<file>`` (one chapter down —
    REF-007's two accepted spellings). Filename -> the id of the first such page, by
    path, sorted for determinism; a later page's reference to an already-claimed
    filename doesn't override the first."""
    indexed_pages = sorted(
        (PurePosixPath(p), rec.id)
        for p, rec in index.records.items()
        if rec.kind is IndexKind.BS_PAGE and PurePosixPath(p).is_relative_to(book_dir)
    )
    out: dict[str, int] = {}
    for path, page_id in indexed_pages:
        full = root / path
        if not full.is_file():
            continue
        for ref in scan_refs(full.read_text(encoding="utf-8")):
            if ref.kind != "embed":
                continue
            name = None
            if ref.path.startswith("images/"):
                name = ref.path[len("images/") :]
            elif ref.path.startswith("../images/"):
                name = ref.path[len("../images/") :]
            if name is not None and name not in out:
                out[name] = page_id
    return out
