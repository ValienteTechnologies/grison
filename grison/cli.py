"""grison CLI — three verbs: ``parse``, ``status``, ``sync``.

The path names the backend (``findings/`` ⇄ Ghostwriter, ``methodology/`` ⇄
BookStack); location decides identity; the first ``sync`` bootstraps the workspace.
``parse`` and ``status`` are offline; ``sync`` reconciles findings with Ghostwriter
and methodology with BookStack (push/pull/collision derived per record).
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, TypeVar

import typer

from grison import gitdrive
from grison.adapters import bs_structure, gw_report
from grison.adapters._bs_common import build_context
from grison.adapters._gw_common import GWContext, GWReportContext
from grison.adapters._gw_common import build_context as build_gw_context
from grison.adapters.bs_images import BsImagesAdapter, page_ids_in_book
from grison.adapters.bs_pages import BsPageAdapter
from grison.adapters.gw_evidence import GwEvidenceAdapter
from grison.adapters.gw_findings import GwLibraryFindingAdapter, GwReportedFindingAdapter
from grison.adapters.gw_notes import ReportNoteAdapter
from grison.adapters.gw_report import NarrativeSectionAdapter
from grison.engine import events as engine_events
from grison.engine.adapter import CreateUndoAdapter
from grison.engine.apply import RunOptions
from grison.engine.apply import run as engine_run
from grison.engine.filesets import RunOptions as FilesetRunOptions
from grison.engine.filesets import rewrite_captions
from grison.engine.filesets import sync_fileset as engine_sync_fileset
from grison.engine.model import Event, KindSummary, Outcome, Plan, RemoteRecord
from grison.engine.offline_status import OfflineStatus, StatusEntry, compute_offline_status
from grison.engine.sidecar import is_sidecar_name
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.engine.undo import describe_snapshot as engine_describe_snapshot
from grison.engine.undo import list_snapshots as engine_list_snapshots
from grison.engine.undo import replay as engine_undo_replay
from grison.engine.undo import snapshot_kinds as engine_snapshot_kinds
from grison.errors import GrisonError
from grison.fsio import atomic_write_text, ensure_private_dir, open_private
from grison.index import Index, IndexKind
from grison.markdown.refscan import scan_refs
from grison.model import FindingType
from grison.remote.bookstack import BookStackClient
from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.compat import SchemaCompatibilityError, check_ghostwriter_compatibility
from grison.remote.creds import Creds, MissingCreds, Settings, load_settings
from grison.remote.creds import load as load_creds
from grison.remote.ghostwriter import GhostwriterClient
from grison.sinks import ParseSummary, run_parse
from grison.validator import find_workspace_root, validate_workspace
from grison.workspace import bootstrap_tree, inbox_dir

app = typer.Typer(
    name="grison",
    help="A markdown hub between security scanners and Ghostwriter + BookStack.",
    no_args_is_help=True,
)


def _make_gw_client(creds: Creds) -> GhostwriterClient:
    """Build the Ghostwriter client — the seam tests monkeypatch to inject a
    transport (``httpx.MockTransport``) instead of hitting the network."""
    return GhostwriterClient(creds)


def _make_bs_client(creds: Creds) -> BookStackClient:
    """Build the BookStack client — same seam as :func:`_make_gw_client`."""
    return BookStackClient(creds)


def _print_version(value: bool) -> None:
    if value:
        from grison import __version__

        typer.echo(__version__)
        raise typer.Exit()


_T2 = TypeVar("_T2")


def _guarded(fn: Callable[..., _T2]) -> Callable[..., _T2]:
    """Wrap a command so any :class:`GrisonError` reaching here prints one plain
    ``error: …`` line to stderr and exits 1, instead of an uncaught traceback (today
    e.g. ``GRISON_GW_URL=http://…`` raises ``HttpConfigError`` straight through
    typer's own rich-traceback handler). A command's own ``typer.Exit`` (its normal
    exit-code signaling) passes through untouched — this only catches what nothing
    else already handled."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> _T2:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except GrisonError as e:
            typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    return wrapper


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    """grison — parse scanner artifacts to markdown, then status/sync with the remotes."""


@app.command()
@_guarded
def parse(
    paths: Annotated[list[Path], typer.Argument(help="Scanner export file(s) or dir(s).")],
    scanner: Annotated[
        str | None,
        typer.Option("--scanner", help="Force a scanner type instead of auto-detecting."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("-o", "--out", help="Output dir (default: findings/inbox/)."),
    ] = None,
    finding_type: Annotated[
        FindingType | None,
        typer.Option("--finding-type", help="Override the per-scanner finding-type default."),
    ] = None,
    min_severity: Annotated[
        str | None,
        typer.Option("--min-severity", help="Keep only e.g. 'high,critical' or 'medium-critical'."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing.")] = False,
) -> None:
    """Turn scanner export(s) into markdown findings in findings/inbox/ (offline)."""
    root = Path.cwd()
    if out is None:
        # The binary scaffolds; no init. A full workspace, not just the bare
        # directory tree (spec §10 / bootstrap.py's own docstring: "a first
        # `grison sync`/`grison parse` in an empty directory yields a complete,
        # valid, self-contained workspace") — bootstrap_workspace is credential-
        # free (it only ever writes a template env, never contacts a remote), so
        # `parse` staying fully offline is unaffected. Without this, `grison parse`
        # in an empty directory left no manifest.yml/index.json behind and the very
        # next `grison validate` exited 2 "no grison workspace found".
        bootstrap_workspace(root)
        out_dir = inbox_dir(root)
    else:
        out_dir = out
    summary = run_parse(
        paths,
        out_dir,
        scanner=scanner,
        finding_type=finding_type,
        min_severity=min_severity,
        dry_run=dry_run,
    )
    _print_parse_summary(summary, out_dir, dry_run=dry_run)
    if not dry_run:
        _git_commit_or_warn(root, load_settings(root), f"grison: parse {_scanner_label(summary)}")
    if summary.errors:
        raise typer.Exit(code=1)


@app.command()
@_guarded
def status(
    remote: Annotated[
        bool,
        typer.Option(
            "--remote",
            help="Also contact BookStack and classify (dry-run, no writes) "
            "what a sync would do — offline otherwise (index + private state + the "
            "validator only).",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Whole-workspace overview: per-area counts, only non-clean paths listed.

    Offline by default — the index, private state, and ``grison validate``'s own
    checks, no credentials, no network: ``methodology/`` and ``findings/reports/``
    (both on the sync engine) get a real per-record breakdown
    (clean/edited/new/deleted/moved/invalid/unknown, plus any live collision sidecar)
    computed from the index and state alone, the same way ``grison sync`` would
    classify locally. ``findings/library`` is not yet engine-managed (a later step)
    and says so, same as before. ``--remote`` additionally contacts BookStack and
    runs the engine's classify step in dry-run mode for the wiki, so the report also
    shows what the next ``grison sync`` would actually do there (will-pull,
    collision, remote-deleted, …).

    "last sync" is read per PHASE from ``.grison/state/last-sync.json`` (each phase —
    findings/report/wiki — records its own outcome there, including a failure), so
    a phase that has never run, or last failed, is never reported as if it were fine.

    Exit code: 0 clean, 1 something needs attention — an invalid record, an unknown
    one (no recorded base to compare against), or a live collision sidecar; an
    ordinary pending edit/new/deleted/moved record is not itself a problem (matches
    ``grison validate``'s own policy — see ``grison sync``'s result/exit-code policy
    in ENGINE.md §10) — 2 could not run (no workspace).
    """
    try:
        root = find_workspace_root(Path.cwd())
    except GrisonError as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None

    failures = validate_workspace(root)
    state = StateStore(root)
    last_sync = state.load_last_sync()
    phases = last_sync.get("phases", {}) if last_sync else {}

    index = Index.load(root)
    findings_failures = [f for f in failures if f.path.startswith("findings")]
    wiki_failures = [f for f in failures if f.path.startswith("methodology")]
    offline = compute_offline_status(root, BsPageAdapter(), index, state, wiki_failures)
    lib_offline = compute_offline_status(
        root,
        GwLibraryFindingAdapter(),
        index,
        state,
        findings_failures,
    )
    rf_offline = compute_offline_status(
        root,
        GwReportedFindingAdapter(index=index, evidence_by_report={}),
        index,
        state,
        findings_failures,
    )
    evidence_counts = {
        d: _fileset_status_counts(root / d / "evidence")
        for d in _report_dirs_for_status(index)
        if (root / d / "evidence").is_dir()
    }
    image_counts = {
        d: _fileset_status_counts(root / d / "images")
        for d in _book_dirs(index)
        if (root / d / "images").is_dir()
    }

    reports_failures = [f for f in failures if f.path.startswith("findings/reports")]
    reports_offline = _merge_offline(
        [
            compute_offline_status(root, NarrativeSectionAdapter(), index, state, reports_failures),
            compute_offline_status(root, ReportNoteAdapter(), index, state, reports_failures),
        ]
    )

    remote_summary: KindSummary | None = None
    remote_error: str | None = None
    if remote:
        creds = load_creds(root)
        if creds.bs_url and creds.bs_token_id and creds.bs_token_secret:
            try:
                with _make_bs_client(creds) as client:
                    ctx = build_context(client, state)
                    dry_index = Index.load(root)  # a scratch copy — dry-run never persists it
                    bs_structure.sync_structure(
                        root,
                        ctx,
                        dry_index,
                        state,
                        Snapshot(),
                        dry_run=True,
                    )
                    _plans, _events, remote_summary = engine_run(
                        root,
                        ctx,
                        BsPageAdapter(),
                        index=dry_index,
                        state=state,
                        snapshot=Snapshot(),
                        failures=wiki_failures,
                        options=RunOptions(dry_run=True),
                    )
            except GrisonError as e:
                remote_error = str(e)
        else:
            remote_error = "BookStack credentials not configured"

    offline_problems = bool(
        offline.counts["invalid"]
        or offline.counts["unknown"]
        or offline.collision_sidecars
        or lib_offline.counts["invalid"]
        or lib_offline.counts["unknown"]
        or rf_offline.counts["invalid"]
        or rf_offline.counts["unknown"]
    )
    reports_problems = bool(
        reports_offline.counts["invalid"]
        or reports_offline.counts["unknown"]
        or reports_offline.collision_sidecars
    )
    problems = (
        offline_problems
        or reports_problems
        or (
            remote_summary is not None
            and any(
                k in remote_summary.counts for k in ("collision", "invalid", "failed", "withheld")
            )
        )
    )

    if json_output:
        payload = {
            "findings": {
                "managed": True,
                "library": {
                    "counts": lib_offline.counts,
                    "non_clean": [_entry_json(e) for e in lib_offline.non_clean],
                },
                "reports": {
                    "counts": rf_offline.counts,
                    "non_clean": [_entry_json(e) for e in rf_offline.non_clean],
                },
            },
            "report": {
                "managed": True,
                "counts": reports_offline.counts,
                "non_clean": [_entry_json(e) for e in reports_offline.non_clean],
                "collision_sidecars": [str(p) for p in reports_offline.collision_sidecars],
                "evidence": {str(d): _fileset_json(c) for d, c in evidence_counts.items()},
            },
            "methodology": {
                "managed": True,
                "counts": offline.counts,
                "non_clean": [_entry_json(e) for e in offline.non_clean],
                "collision_sidecars": [str(p) for p in offline.collision_sidecars],
                "images": {str(d): _fileset_json(c) for d, c in image_counts.items()},
            },
            "remote": (
                {"counts": remote_summary.counts, "problem_paths": remote_summary.problem_paths}
                if remote_summary is not None
                else remote_error
            ),
            "last_sync": {
                "findings": phases.get("findings"),
                "report": phases.get("report"),
                "wiki": phases.get("wiki"),
            },
        }
        typer.echo(json.dumps(payload, indent=2))
        if problems:
            raise typer.Exit(code=1)
        return

    lib_line = ", ".join(f"{b} {n}" for b, n in lib_offline.counts.items() if n)
    rf_line = ", ".join(f"{b} {n}" for b, n in rf_offline.counts.items() if n)
    typer.echo(f"findings (library): {lib_line or 'clean'}")
    typer.echo(f"findings (reports): {rf_line or 'clean'}")
    reports_counts_line = ", ".join(f"{b} {n}" for b, n in reports_offline.counts.items() if n)
    typer.echo(f"report: {reports_counts_line or 'clean'}")
    if evidence_counts:
        typer.echo(f"evidence: {_fileset_status_line(evidence_counts)}")
    _print_offline_non_clean(reports_offline, area="report")
    counts_line = ", ".join(f"{b} {n}" for b, n in offline.counts.items() if n)
    typer.echo(f"methodology: {counts_line or 'clean'}")
    if image_counts:
        typer.echo(f"images: {_fileset_status_line(image_counts)}")
    _print_offline_non_clean(offline, area="methodology")
    if remote_summary is not None:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(remote_summary.counts.items()))
        typer.echo(f"remote (--remote, dry-run): {counts}")
        for problem_path in remote_summary.problem_paths:
            typer.secho(f"  ! {problem_path}", fg=typer.colors.RED)
    elif remote_error is not None:
        typer.secho(f"remote: {remote_error}", fg=typer.colors.YELLOW)
    for phase in ("findings", "report", "wiki"):
        info = phases.get(phase)
        if info is None:
            typer.echo(f"last {phase} sync: never")
        else:
            tag = "ok" if info.get("ok") else f"FAILED — {info.get('error', 'see prior output')}"
            typer.echo(f"last {phase} sync: {info.get('at', '?')} ({tag})")

    if problems:
        raise typer.Exit(code=1)


def _entry_json(e: StatusEntry) -> dict[str, Any]:
    return {
        "path": str(e.path),
        "bucket": e.bucket,
        "reasons": list(e.reasons),
        "moved_from": str(e.moved_from) if e.moved_from is not None else None,
    }


@dataclass(frozen=True)
class _FilesetCounts:
    """One file-set folder's offline-visible shape (evidence/images, item 5): a
    live collision sidecar (``grison.engine.sidecar.is_sidecar_name``) is listed
    as a collision, never counted as a file — mirrors the same sidecar-aware
    treatment ``grison sync``/``last-sync.json`` already give it, so ``grison
    status`` never shows an extra "file" a real sync wouldn't otherwise write."""

    files: int
    collisions: int


def _fileset_status_counts(dir_path: Path) -> _FilesetCounts:
    files = collisions = 0
    for p in sorted(dir_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if is_sidecar_name(p.name):
            collisions += 1
        else:
            files += 1
    return _FilesetCounts(files=files, collisions=collisions)


def _fileset_json(c: _FilesetCounts) -> dict[str, int]:
    return {"files": c.files, "collisions": c.collisions}


def _fileset_status_line(counts: dict[PurePosixPath, _FilesetCounts]) -> str:
    parts = []
    for d, c in sorted(counts.items()):
        part = f"{d} {c.files}"
        if c.collisions:
            part += f" ({c.collisions} collision-sidecar(s) pending)"
        parts.append(part)
    return ", ".join(parts)


def _merge_offline(statuses: list[OfflineStatus]) -> OfflineStatus:
    """Combine per-kind offline statuses (``findings/reports`` has two engine-managed
    kinds, ``gw.reportSection`` and ``gw.projectNote``) into one view for `grison
    status` — every rule already files its own failure/bucket per path, so a plain
    concatenation is exact, never a re-derived summary."""
    entries = sorted((e for s in statuses for e in s.entries), key=lambda e: str(e.path))
    sidecars = sorted({p for s in statuses for p in s.collision_sidecars})
    return OfflineStatus(entries=entries, collision_sidecars=sidecars)


def _print_offline_non_clean(offline: OfflineStatus, *, area: str) -> None:
    if offline.collision_sidecars:
        typer.echo(f"{area}: {len(offline.collision_sidecars)} collision-sidecar(s) pending")
        for sidecar in offline.collision_sidecars:
            typer.secho(
                f"  ! {sidecar}: unresolved collision — run `grison sync "
                "--force-local`/`--force-remote`",
                fg=typer.colors.RED,
            )
    for entry in offline.non_clean:
        color = typer.colors.RED if entry.bucket in ("invalid", "unknown") else None
        detail = ""
        if entry.bucket == "invalid":
            detail = f" ({', '.join(entry.reasons)})"
        elif entry.bucket == "moved":
            detail = f" (from {entry.moved_from})"
        elif entry.bucket == "unknown":
            detail = " (no recorded base — never synced through this state store)"
        typer.secho(f"  {entry.bucket:8} {entry.path}{detail}", fg=color, dim=color is None)


@app.command()
@_guarded
def validate(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Only these files/dirs (plus the cross-file rules they touch) — "
            "default: the whole workspace."
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable, machine-readable JSON array instead."),
    ] = False,
    deleted_ok: Annotated[
        bool,
        typer.Option(
            "--deleted-ok",
            help="A given path that no longer exists still validates "
            "its containing directory's cross-file rules (for a post-edit hook running "
            "after a delete), instead of failing with 'no such path'.",
        ),
    ] = False,
) -> None:
    """Validate the workspace against format v2 — offline, no credentials, no network.

    Runs from anywhere inside the workspace (walks up to find ``.grison/``, like
    ``git`` finds ``.git/``); ``PATHS`` are resolved relative to the current
    directory, then expressed relative to the workspace root. Quiet on success. One
    line per failure: ``path:line: RULE-ID message — fix``.

    Exit code: ``0`` the workspace is clean; ``1`` the validator ran fine and found
    one or more real document problems; ``2`` the validator itself could not run at
    all (a usage error — including a given path that doesn't exist, is outside the
    workspace, or isn't a validated location — or an unexpected internal failure) —
    so a pre-commit hook or CI step can tell "your documents are invalid" (1, fix the
    documents) apart from "validate itself is broken" (2, fix grison / the
    invocation), instead of both looking like the same failure. A ``PATHS`` entry
    that matches nothing NEVER silently exits 0 — see §9 of the workspace-format spec.
    """
    try:
        root = find_workspace_root(Path.cwd())
        failures = validate_workspace(root, paths=paths, deleted_ok=deleted_ok)
    except GrisonError as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    except Exception as e:  # noqa: BLE001 — "could not run" must never look like "passed"
        typer.secho(f"error: validator failed to run: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "rule_id": f.rule_id,
                        "path": f.path,
                        "line": f.line,
                        "message": f.message,
                        "fix": f.fix,
                    }
                    for f in failures
                ],
                indent=2,
            )
        )
    else:
        for f in failures:
            loc = f"{f.path}:{f.line}" if f.line is not None else f.path
            typer.echo(f"{loc}: {f.rule_id} {f.message} — {f.fix}")
    if failures:
        raise typer.Exit(code=1)


@app.command()
@_guarded
def sync(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Contact Ghostwriter/BookStack and preview the sync plan, write nothing "
            "(unlike `status`, which is offline and findings-only).",
        ),
    ] = False,
    force_local: Annotated[
        Path | None,
        typer.Option("--force-local", help="Resolve a file's collision by taking local (push)."),
    ] = None,
    force_remote: Annotated[
        Path | None,
        typer.Option("--force-remote", help="Resolve a file's collision by taking remote (pull)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the wiki phase's events as JSON lines (one object "
            "per event plus a final summary object). The findings/report phases are "
            "not yet engine-managed and keep their existing text output either way.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Also show INFO-severity skips (drafts, templates, a "
            "wysiwyg page grison has never managed) — hidden by default since there is "
            "nothing to do about them; always present in --json regardless.",
        ),
    ] = False,
) -> None:
    """Reconcile the workspace with Ghostwriter + BookStack — push/pull/collision
    derived per record.

    Bootstraps on first run. Direction isn't chosen: a locally-edited record pushes, a
    remote-changed one pulls, and a record changed on both sides is surfaced (never
    overwritten). Every remote write is snapshot-backed: the whole run (report,
    findings and wiki phases alike) shares ONE undo snapshot, so a single ``grison
    undo`` afterwards reverses every remote write this run made, newest first —
    never just the last phase's. ``--force-local``/``--force-remote`` accept a path
    under ``methodology/`` (routed to the wiki engine) or under ``findings/`` (routed
    to the older findings/report phases).
    """
    root = Path.cwd()
    boot = bootstrap_workspace(root)
    creds = load_creds(root)
    settings = load_settings(root)
    try:
        creds.require_ghostwriter()
    except MissingCreds as e:
        if boot.env_created:
            typer.secho(f"Scaffolded workspace + wrote {boot.env_path}", fg=typer.colors.GREEN)
        if boot.claude_md_created:
            typer.secho("Scaffolded workspace + wrote CLAUDE.md", fg=typer.colors.GREEN)
        typer.secho(str(e), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from None

    fl = {force_local.resolve()} if force_local else set()
    fr = {force_remote.resolve()} if force_remote else set()
    # Each phase (findings / reports / wiki) is isolated: a raised exception in one is
    # recorded and printed, never left to silently cancel the phases after it.
    phase_errors: list[str] = []
    wiki: WikiPhaseResult | None = None
    # ONE undo snapshot for the whole run (not one per phase — grison.engine.undo's
    # module docstring, "the user's mental model is 'undo the last sync'"): every
    # phase below appends its remote-write preimages to this SAME Snapshot. It is
    # checkpointed (persisted in place) after EACH phase that ran, not only once at
    # the very end — a crash/Ctrl-C/uncaught error between phases must not lose an
    # earlier phase's already-applied remote writes from the undo record. `run_at` is
    # fixed once, up front, so every checkpoint of this run writes the SAME
    # `.grison/snapshots/<run_at>/` directory (Snapshot.persist's `at` param) —
    # overwriting ops.json with the fuller list so far, never creating a second
    # directory. Still nothing is written for a pull-only run (the "no snapshot" rule
    # — `not snapshot.empty` guards every checkpoint call).
    snapshot = Snapshot()
    run_at = datetime.now(UTC)
    snapshot_dir: Path | None = None

    def _checkpoint_snapshot() -> None:
        nonlocal snapshot_dir
        if not dry_run and not snapshot.empty:
            snapshot_dir = snapshot.persist(root, at=run_at)

    with _workspace_lock(root):  # one sync at a time per workspace (GW has no compare-and-swap)
        if not dry_run:  # checkpoint whatever was dirty before we touch anything
            _git_commit_or_warn(root, settings, "grison: pre-sync checkpoint")
        with _make_gw_client(creds) as client:
            # ENGINE.md "check server compatibility" — BEFORE the first fetch of any
            # phase: one cheap fingerprint request compared against
            # .grison/state/schema.json; only a changed/absent fingerprint pays for
            # the full introspection + validate-every-operation pass (BRIEF task F —
            # see grison.remote.compat's module docstring). A mismatch here is
            # "could not run" (exit 2), never a per-phase failure, so it must never
            # be swallowed by `_run_phase`.
            try:
                check_ghostwriter_compatibility(client, root)
            except SchemaCompatibilityError as e:
                typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from None

            # Report BEFORE findings: a brand-new report only becomes an indexed
            # gw.report directory during the report phase -- a reported finding
            # belonging to it can only resolve/land in that directory (BRIEF D:
            # "a reported finding belongs to the report whose indexed directory
            # it sits in") once that indexing has happened, so this order lets a
            # first sync pull a report AND its findings in one run instead of
            # needing two. The report phase now ALSO syncs every report's
            # evidence/ file set, before findings (D1: "replacing an image's
            # bytes must re-push every finding referencing it, automatically, in
            # the same run" — see _run_reports_phase's docstring) — its result
            # carries the one evidence_by_report the findings phase's own
            # GwReportedFindingAdapter needs, built once and shared, never a
            # second org-wide evidence fetch.
            rep_and_evidence = _run_phase(
                "report",
                lambda: _run_reports_phase(
                    root,
                    client,
                    dry_run=dry_run,
                    force_local=fl,
                    force_remote=fr,
                    snapshot=snapshot,
                ),
                phase_errors,
            )
            rep, evidence_by_report = (
                rep_and_evidence if rep_and_evidence is not None else (None, {})
            )
            _checkpoint_snapshot()  # durable now, even if findings/wiki never run
            result = _run_phase(
                "findings",
                lambda: _run_findings_phase(
                    root,
                    client,
                    dry_run=dry_run,
                    force_local=fl,
                    force_remote=fr,
                    evidence_by_report=evidence_by_report,
                    snapshot=snapshot,
                ),
                phase_errors,
            )
            _checkpoint_snapshot()  # durable now, even if the wiki phase never runs
        findings_ok = result is not None and result.exit_code == 0
        report_ok = rep is not None and rep.exit_code == 0
        bad = (
            bool(phase_errors)
            or (result is not None and not findings_ok)
            or (rep is not None and not report_ok)
        )

        if creds.bs_url and creds.bs_token_id and creds.bs_token_secret:

            def _do_wiki() -> WikiPhaseResult:
                with _make_bs_client(creds) as bs:
                    return _run_wiki_phase(
                        root,
                        bs,
                        dry_run=dry_run,
                        force_local=fl,
                        force_remote=fr,
                        snapshot=snapshot,
                    )

            wiki = _run_phase("wiki", _do_wiki, phase_errors)
            _checkpoint_snapshot()  # final checkpoint — wiki was the last phase to append
            if wiki is not None:
                bad = bad or wiki.exit_code != 0
        bad = bad or bool(phase_errors)  # catches a wiki-phase failure too

        if result is not None:
            result.snapshot_dir = snapshot_dir
        if rep is not None:
            rep.snapshot_dir = snapshot_dir
        if wiki is not None:
            wiki.snapshot_dir = snapshot_dir

        if result is not None:
            if json_output:
                typer.echo(_findings_json(result))
            else:
                _print_findings_summary(result, dry_run=dry_run, verbose=verbose)
        if rep is not None:
            if json_output:
                typer.echo(_reports_json(rep))
            else:
                _print_reports_summary(rep, dry_run=dry_run, verbose=verbose)
        if wiki is not None:
            if json_output:
                typer.echo(_wiki_json(wiki))
            else:
                _print_wiki_summary(wiki, dry_run=dry_run, verbose=verbose)
        if not json_output and snapshot_dir is not None:
            typer.echo(f"snapshot: {snapshot_dir}")

        if not dry_run:  # capturing state is the point, even (especially) after failures
            _git_commit_or_warn(root, settings, _git_sync_message(bad, result, rep, wiki))
            # item 6 shim: every phase that actually RAN records its own outcome in
            # last-sync.json, including a failing one — `grison status` reads this to
            # say when each phase last ran and whether it succeeded, which was
            # previously true only for wiki (the only phase that wrote here at all).
            if result is not None or _phase_error(phase_errors, "findings") is not None:
                _record_phase_last_sync(
                    root,
                    "findings",
                    ok=findings_ok,
                    error=_phase_error(phase_errors, "findings"),
                    summary=_findings_last_sync_summary(result) if result is not None else None,
                )
            if rep is not None or _phase_error(phase_errors, "report") is not None:
                _record_phase_last_sync(
                    root,
                    "report",
                    ok=report_ok,
                    error=_phase_error(phase_errors, "report"),
                    summary=_reports_last_sync_summary(rep) if rep is not None else None,
                )
            if wiki is not None or _phase_error(phase_errors, "wiki") is not None:
                _record_phase_last_sync(
                    root,
                    "wiki",
                    ok=wiki is not None and wiki.exit_code == 0,
                    error=_phase_error(phase_errors, "wiki"),
                    summary=_wiki_last_sync_summary(wiki) if wiki is not None else None,
                )

    if bad:
        raise typer.Exit(code=1)


def _phase_error(phase_errors: list[str], name: str) -> str | None:
    prefix = f"{name} sync failed: "
    return next((m[len(prefix) :] for m in phase_errors if m.startswith(prefix)), None)


def _reports_last_sync_summary(rep: ReportsPhaseResult) -> dict[str, Any]:
    return {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in rep.summaries.items()
        },
        "dirs": {
            "created": rep.dirs.created,
            "materialized": rep.dirs.materialized,
            "scope_failures": len(rep.dirs.scope_failures),
            "errors": len(rep.dirs.errors),
        },
        "snapshot_dir": str(rep.snapshot_dir) if rep.snapshot_dir else None,
    }


def _wiki_last_sync_summary(wiki: WikiPhaseResult) -> dict[str, Any]:
    return {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in wiki.summaries.items()
        },
        "snapshot_dir": str(wiki.snapshot_dir) if wiki.snapshot_dir else None,
    }


def _record_phase_last_sync(
    root: Path,
    phase: str,
    *,
    ok: bool,
    error: str | None,
    summary: dict[str, Any] | None,
) -> None:
    """``.grison/state/last-sync.json`` (ENGINE.md 'State') — read by ``grison
    status``: ``{"phases": {"findings": {...}, "report": {...}, "wiki": {...}}}``, one
    entry per phase that has ever run, each independently updated so one phase's
    outcome never clobbers another's. A failing phase (``error`` set, ``summary``
    ``None``) still gets an entry — `grison status`'s "last sync" line must never say
    a broken phase looks fine just because it never wrote anything."""
    state = StateStore(root)
    payload = state.load_last_sync() or {}
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        phases = {}
    entry: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "ok": ok}
    if error is not None:
        entry["error"] = error
    if summary is not None:
        entry["summary"] = summary
    phases[phase] = entry
    payload["phases"] = phases
    state.save_last_sync(payload)


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


def _findings_relative_force_set(root: Path, paths: set[Path]) -> frozenset[PurePosixPath]:
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "findings":
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


@dataclass
class FindingsPhaseResult:
    """The findings phase's result: library findings + reported findings, both
    through :mod:`grison.engine.apply`. Evidence file sets sync in the REPORTS
    phase now, before this one — see :func:`grison.cli._run_reports_phase`'s
    docstring (D1: a reupload's re-push must land in the same run as the
    reupload, which requires evidence to sync before whatever references it) —
    so ``ReportsPhaseResult``, not this one, carries evidence's own plans/
    summaries/events."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    snapshot_dir: Path | None = None

    @property
    def exit_code(self) -> int:
        return 1 if any(p.is_problem for p in self.plans) else 0


def _report_finding_bodies(root: Path, report_dir: PurePosixPath) -> dict[PurePosixPath, str]:
    """Every reported-finding document currently on disk in ``report_dir`` (top
    level only — narrative/notes/evidence are different record types), for
    :func:`grison.engine.filesets.collect_captions`'s caption-agreement scan."""
    out: dict[PurePosixPath, str] = {}
    d = root / report_dir
    if not d.is_dir():
        return out
    for md in d.glob("*.md"):
        if md.name.endswith(".remote.md"):
            continue
        out[PurePosixPath(md.relative_to(root).as_posix())] = md.read_text(encoding="utf-8")
    return out


def _apply_caption_rewrites(
    root: Path,
    doc_bodies: dict[PurePosixPath, str],
    resolved: dict[str, tuple[str, str]],
    *,
    folder_name: str,
) -> None:
    """The PULL-side half of D1's caption rule (module docstring of
    :mod:`grison.engine.filesets`): rewrite every embed's alt/title in every
    referencing document to match the evidence set's resolved caption. Never
    touches state/index — a referencing document's canonical payload excludes
    captions by construction, so this can never make it look edited."""
    for path, body in doc_bodies.items():
        rewritten = rewrite_captions(body, resolved, folder_name=folder_name)
        if rewritten != body:
            atomic_write_text(root / path, rewritten)


def _run_findings_phase(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    evidence_by_report: dict[int, dict[int, dict[str, Any]]],
    snapshot: Snapshot,
) -> FindingsPhaseResult:
    """The findings phase (BRIEF engine step 3): library findings, then reported
    findings across every report (one adapter/kind — see
    :mod:`grison.adapters.gw_findings`'s module docstring on cross-report moves).
    Evidence file sets no longer sync here — see :func:`_run_reports_phase`'s
    docstring (D1) — so ``evidence_by_report`` (that phase's own result, built
    ONCE right after its evidence-file-set sync) is a required parameter, not
    something this phase re-fetches; the fresh-index dependency that made "report
    phase, then findings phase" work at all (a reupload's new id, or a brand-new
    report directory, must already be on disk before this phase's own
    ``Index.load`` below) is unchanged — it was always ``grison.cli.sync``'s
    sequential phase calls (each phase persists its index before the next one
    loads), never anything specific to where evidence used to sync. Validation is
    scoped to ``findings/`` here, same pattern as the wiki phase's own
    ``methodology/`` scoping.

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot` — the same
    object the report and (if it runs) wiki phases also append to and that
    ``sync`` alone persists, once, after every phase has run (one undo snapshot
    per sync run, not one per phase — see :mod:`grison.engine.undo`'s module
    docstring). This phase never persists it and never creates its own."""
    index = Index.load(root)
    state = StateStore(root)
    ctx = GWContext.build(client, index)

    all_failures = validate_workspace(root, paths=[root / "findings"])
    findings_failures = [f for f in all_failures if f.path.startswith("findings")]

    fl = _findings_relative_force_set(root, force_local)
    fr = _findings_relative_force_set(root, force_remote)
    options = RunOptions(dry_run=dry_run, force_local=fl, force_remote=fr)

    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}

    lib_plans, lib_events, lib_summary = engine_run(
        root,
        client,
        GwLibraryFindingAdapter(),
        index=index,
        state=state,
        snapshot=snapshot,
        failures=findings_failures,
        options=options,
    )
    events.extend(lib_events)
    summaries[GwLibraryFindingAdapter.kind] = lib_summary

    rf_adapter = GwReportedFindingAdapter(index=index, evidence_by_report=evidence_by_report)
    rf_plans, rf_events, rf_summary = engine_run(
        root,
        ctx,
        rf_adapter,
        index=index,
        state=state,
        snapshot=snapshot,
        failures=findings_failures,
        options=options,
    )
    events.extend(rf_events)
    summaries[GwReportedFindingAdapter.kind] = rf_summary

    if not dry_run:
        index.save()

    return FindingsPhaseResult(
        plans=[*lib_plans, *rf_plans],
        events=events,
        summaries=summaries,
    )


def _findings_json(findings: FindingsPhaseResult) -> str:
    summary = {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in findings.summaries.items()
        },
        "snapshot_dir": str(findings.snapshot_dir) if findings.snapshot_dir else None,
        "exit_code": findings.exit_code,
    }
    return engine_events.render_json(findings.events, summary=summary)


def _print_findings_summary(
    findings: FindingsPhaseResult,
    *,
    dry_run: bool,
    verbose: bool = False,
) -> None:
    for line in engine_events.render_text_lines(findings.events, verbose=verbose):
        color = None
        if line.startswith(("collision", "invalid", "failed", "withheld")):
            color = typer.colors.RED
        elif line.startswith("skip"):
            color = typer.colors.YELLOW
        typer.secho(line, fg=color, dim=color is None)
    for kind, summary in findings.summaries.items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.counts.items()))
        typer.secho(f"findings ({kind}): {counts or 'clean'}", fg=typer.colors.GREEN)
        if summary.counts.get("withheld"):
            typer.secho(
                f"MASS-CHANGE GUARD tripped on {kind} — writes withheld.", fg=typer.colors.RED
            )
    # No per-phase "snapshot: ..." line here: one sync run shares ONE snapshot across
    # every phase (grison.cli.sync persists it once, after the last phase runs, and
    # prints it once itself) — see grison.engine.undo's module docstring.


def _findings_last_sync_summary(findings: FindingsPhaseResult) -> dict[str, Any]:
    return {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in findings.summaries.items()
        },
        "snapshot_dir": str(findings.snapshot_dir) if findings.snapshot_dir else None,
    }


def _wiki_relative_force_set(root: Path, paths: set[Path]) -> frozenset[PurePosixPath]:
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "methodology":
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


def _run_wiki_phase(
    root: Path,
    client: BookStackClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    snapshot: Snapshot,
) -> WikiPhaseResult:
    """The wiki phase: BookStack structure (books/chapters/shelves — creates any new
    local book/chapter directory, then regenerates the read-only mirrors), then pages
    through :mod:`grison.engine.apply`. The validation gate is scoped to
    ``methodology/`` here (task step 1's scope parameter — findings/reports are still
    format v1 and would fail v2 validation wholesale); pulls are never blocked by it,
    only pushes/creates/deletes (see ``grison.engine.apply``'s own gate).

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot`, shared with
    the report and findings phases that already ran — this phase's own writes
    (structure, images, pages) append to it too, never their own snapshot; ``sync``
    persists it once, after this (the last) phase runs."""
    index = Index.load(root)
    state = StateStore(root)
    ctx = build_context(client, state)
    ctx.indexed_page_ids = frozenset(
        rec.id for rec in index.records.values() if rec.kind.value == BsPageAdapter.kind
    )

    all_failures = validate_workspace(root, paths=[root / "methodology"])
    wiki_failures = [f for f in all_failures if f.path.startswith("methodology")]

    structure = bs_structure.sync_structure(
        root,
        ctx,
        index,
        state,
        snapshot,
        dry_run=dry_run,
        on_event=lambda msg: typer.secho(msg, dim=True),
    )

    options = RunOptions(
        dry_run=dry_run,
        force_local=_wiki_relative_force_set(root, force_local),
        force_remote=_wiki_relative_force_set(root, force_remote),
    )
    fs_options = FilesetRunOptions(
        dry_run=dry_run,
        force_local=_wiki_relative_force_set(root, force_local),
        force_remote=_wiki_relative_force_set(root, force_remote),
    )

    # D9/BRIEF task C: each book's images/ folder BEFORE its pages, so a fresh
    # upload's gallery URL is visible to the page push that follows (same
    # ordering reason as gw.evidence-before-gw.reportSection, see
    # _run_reports_phase).
    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}
    image_plans: list[Plan] = []
    for book_dir, book_id in sorted(_book_dirs(index).items()):
        page_ids = page_ids_in_book(client, ctx, book_id)
        book_pages = [p for p in ctx.client.fetch_pages() if p["id"] in page_ids]
        anchor_page_id = min((p["id"] for p in book_pages), default=None)
        images_adapter = BsImagesAdapter(
            book_id=book_id,
            page_ids=page_ids,
            anchor_page_id=anchor_page_id,
            anchor_for=_anchor_for_book(root, index, book_dir),
        )
        result = engine_sync_fileset(
            root,
            ctx,
            images_adapter,
            book_dir / "images",
            index=index,
            state=state,
            snapshot=snapshot,
            options=fs_options,
            failures=wiki_failures,
        )
        events.extend(result.events)
        summaries[f"bs.image[{book_dir}]"] = result.summary
        image_plans.extend(result.plans)

    plans, page_events, summary = engine_run(
        root,
        ctx,
        BsPageAdapter(),
        index=index,
        state=state,
        snapshot=snapshot,
        failures=wiki_failures,
        options=options,
    )
    events.extend(page_events)
    summaries[BsPageAdapter.kind] = summary
    plans = [*image_plans, *plans]

    if not dry_run:
        index.save()

    return WikiPhaseResult(
        plans=plans,
        events=events,
        summaries=summaries,
        structure=structure,
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


def _report_dirs_for_status(index: Index) -> dict[PurePosixPath, int]:
    return {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind is IndexKind.GW_REPORT
    }


def _wiki_json(wiki: WikiPhaseResult) -> str:
    summary = {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in wiki.summaries.items()
        },
        "structure": {
            "created_books": wiki.structure.created_books,
            "created_chapters": wiki.structure.created_chapters,
            "materialized": wiki.structure.materialized,
            "skipped": wiki.structure.skipped,
            "errors": wiki.structure.errors,
        },
        "snapshot_dir": str(wiki.snapshot_dir) if wiki.snapshot_dir else None,
        "exit_code": wiki.exit_code,
    }
    return engine_events.render_json(wiki.events, summary=summary)


def _print_wiki_summary(wiki: WikiPhaseResult, *, dry_run: bool, verbose: bool = False) -> None:
    for line in engine_events.render_text_lines(wiki.events, verbose=verbose):
        color = None
        if line.startswith(("collision", "invalid", "failed", "withheld")):
            color = typer.colors.RED
        elif line.startswith("skip"):
            color = typer.colors.YELLOW
        typer.secho(line, fg=color, dim=color is None)
    for kind, summary in wiki.summaries.items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.counts.items()))
        typer.secho(f"wiki ({kind}): {counts}", fg=typer.colors.GREEN)
        if summary.counts.get("withheld"):
            typer.secho(
                f"MASS-CHANGE GUARD tripped on {kind} — writes withheld.", fg=typer.colors.RED
            )
    st = wiki.structure
    if st.created_books or st.created_chapters:
        typer.echo(
            f"structure: create {len(st.created_books)} book(s), "
            f"{len(st.created_chapters)} chapter(s)"
        )
    if st.materialized:
        typer.echo(f"structure: mirror {len(st.materialized)} book/chapter/shelf file(s)")
    for path, reason in st.skipped:
        typer.secho(f"skipped  {path}: {reason}", fg=typer.colors.YELLOW)
    for e in st.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)
    # No per-phase "snapshot: ..." line here — see _print_findings_summary's comment.


@dataclass
class ReportsPhaseResult:
    """The reports phase's result: report-directory structure (create + mirrors +
    missing-scope trip-wire), plus every :class:`~grison.engine.model.Plan` reached
    across every kind this phase now owns — ``gw.evidence[<report_dir>]`` (one
    file-set summary per report; see :mod:`grison.engine.filesets`),
    ``gw.reportSection``, ``gw.projectNote``.

    D1 ("replacing an image's bytes must re-push every finding referencing it,
    automatically, in the same run"): evidence file sets sync HERE, before
    narrative sections, not in the findings phase — a report's ``evidence/``
    folder belongs to the report, and a narrative section's own re-push (on a
    reupload) must land in the SAME run as the reupload, which only holds if
    the reupload has ALREADY happened by the time ``NarrativeSectionAdapter``
    classifies. The evidence plans/summaries/events counted here (not in
    ``FindingsPhaseResult``) is a deliberate choice, not an accident of where
    the code physically runs — see :func:`_run_reports_phase`'s docstring for
    the exact reasoning and where `grison status`/``--json`` surface it."""

    plans: list[Plan] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    summaries: dict[str, KindSummary] = field(default_factory=dict)
    dirs: gw_report.ReportDirResult = field(default_factory=gw_report.ReportDirResult)
    snapshot_dir: Path | None = None

    @property
    def exit_code(self) -> int:
        if any(p.is_problem for p in self.plans) or self.dirs.errors or self.dirs.scope_failures:
            return 1
        return 0


def _reports_relative_force_set(root: Path, paths: set[Path]) -> frozenset[PurePosixPath]:
    out: set[PurePosixPath] = set()
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts[:2] == ("findings", "reports"):
            out.add(PurePosixPath(rel.as_posix()))
    return frozenset(out)


def _run_reports_phase(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool,
    force_local: set[Path],
    force_remote: set[Path],
    snapshot: Snapshot,
) -> tuple[ReportsPhaseResult, dict[int, dict[int, dict[str, Any]]]]:
    """The reports phase: report directories + `.report.yml`/`project.md` mirrors
    (:func:`grison.adapters.gw_report.sync_report_dirs` — structure-style, like the
    wiki's book/chapter pass); THEN, per indexed report, its ``evidence/`` file set
    (:mod:`grison.engine.filesets` — moved here from the findings phase, D1: "a
    reupload must re-push every finding referencing it, automatically, in the same
    run" — this only holds for a NARRATIVE section's own reupload-triggered re-push
    if the reupload has already happened by the time ``NarrativeSectionAdapter``
    classifies, and the reports phase runs before the findings phase, so evidence
    has to sync here to land in the SAME run at all); THEN narrative sections and
    project notes through the engine, using the evidence rows the fileset sync just
    established (``evidence_by_report``, returned alongside the result — the
    findings phase's ``GwReportedFindingAdapter`` needs the exact same rows, built
    only ONCE here and shared rather than re-fetched, per the coordinator's
    instruction). The validation gate is scoped to ``findings/reports``
    (findings/library and findings/inbox are still format v1 — the findings phase,
    not this one).

    Evidence's own plans/summaries/events land in THIS phase's result (a deliberate
    choice — the alternative, folding them into ``FindingsPhaseResult`` instead, was
    considered and rejected: evidence now sync BEFORE, not alongside, the findings
    engine runs, so counting them there would misdescribe when/where the work
    happened; ``grison status``/``--json`` read ``ReportsPhaseResult.summaries``'s
    ``gw.evidence[<report_dir>]`` keys exactly as before, just attributed to the
    "report" phase instead of "findings" in the per-phase last-sync bookkeeping).

    ``snapshot`` is ``grison.cli.sync``'s ONE run-wide :class:`Snapshot` — the
    findings and (if it runs) wiki phases append to the SAME object; this phase
    (the first one to run) never persists it and never creates its own (one undo
    snapshot per sync run, not one per phase — see :mod:`grison.engine.undo`'s
    module docstring)."""
    index = Index.load(root)
    state = StateStore(root)
    ctx = build_gw_context(client, index)

    dirs = gw_report.sync_report_dirs(
        root,
        ctx,
        index,
        state,
        snapshot,
        dry_run=dry_run,
        on_event=lambda msg: typer.secho(msg, dim=True),
    )
    _refresh_report_dirs(ctx, index)

    all_failures = validate_workspace(root, paths=[root / "findings" / "reports"])
    report_failures = [f for f in all_failures if f.path.startswith("findings/reports")]

    fl = _reports_relative_force_set(root, force_local)
    fr = _reports_relative_force_set(root, force_remote)
    options = RunOptions(dry_run=dry_run, force_local=fl, force_remote=fr)
    fs_options = FilesetRunOptions(dry_run=dry_run, force_local=fl, force_remote=fr)

    plans: list[Plan] = []
    events: list[Event] = []
    summaries: dict[str, KindSummary] = {}

    # --- evidence file sets, one per indexed report (D1 — see docstring above) ---
    evidence_ctx = GWContext.build(client, index)
    for report_dir in sorted(evidence_ctx.report_dirs):
        report_id = evidence_ctx.report_dirs[report_dir]
        evidence_adapter = GwEvidenceAdapter(report_id=report_id)
        doc_bodies = _report_finding_bodies(root, report_dir)
        evidence_dir = report_dir / "evidence"
        try:
            fs_result = engine_sync_fileset(
                root,
                evidence_ctx,
                evidence_adapter,
                evidence_dir,
                index=index,
                state=state,
                snapshot=snapshot,
                doc_bodies=doc_bodies,
                options=fs_options,
                failures=report_failures,
            )
        except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5):
            # one report's evidence file set blowing up must not abort every other
            # report/section in this phase, any more than one record's own apply
            # step does inside grison.engine.filesets/apply themselves.
            reason = f"{type(e).__name__}: {e}"
            events.append(Event(verb="failed", path=str(evidence_dir), detail=reason))
            plans.append(
                Plan(kind=evidence_adapter.kind, outcome=Outcome.FAILED, path=evidence_dir,
                    reason=reason)
            )
            summaries[f"gw.evidence[{report_dir}]"] = KindSummary(
                kind=evidence_adapter.kind, counts={"failed": 1},
                problem_paths=[str(evidence_dir)],
            )
            continue
        # a fileset's own problems (collision/failed/withheld evidence) must count
        # toward THIS phase's exit code exactly like a section's would — folded
        # into the SAME plans list ReportsPhaseResult.exit_code reads, not just
        # reported as text/JSON events that a bad run's exit code would then miss.
        plans.extend(fs_result.plans)
        events.extend(fs_result.events)
        summaries[f"gw.evidence[{report_dir}]"] = fs_result.summary
        if not dry_run and fs_result.resolved_captions:
            _apply_caption_rewrites(
                root, doc_bodies, fs_result.resolved_captions, folder_name="evidence"
            )

    # Evidence rows, re-fetched fresh (post-write) for every report, so the
    # narrative/findings adapters' RefResolvers see ids/captions/friendly-names as
    # they actually are right now, not as of the start of this sync — built ONCE
    # here and returned for the findings phase to reuse (never a second org-wide
    # fetch for the same data).
    evidence_by_report: dict[int, dict[int, dict[str, Any]]] = {}
    for report_id in evidence_ctx.report_dirs.values():
        rows = GwEvidenceAdapter(report_id=report_id).list_remote(evidence_ctx)
        evidence_by_report[report_id] = {i: r.data for i, r in rows.items()}

    # --- narrative sections + project notes ---------------------------------
    for adapter in (
        NarrativeSectionAdapter(evidence_by_report=evidence_by_report), ReportNoteAdapter(),
    ):
        p, adapter_events, s = engine_run(
            root,
            ctx,
            adapter,
            index=index,
            state=state,
            snapshot=snapshot,
            failures=report_failures,
            options=options,
        )
        plans.extend(p)
        events.extend(adapter_events)
        summaries[adapter.kind] = s

    if not dry_run:
        index.save()

    result = ReportsPhaseResult(
        plans=plans,
        events=events,
        summaries=summaries,
        dirs=dirs,
    )
    return result, evidence_by_report


def _refresh_report_dirs(ctx: GWReportContext, index: Index) -> None:
    """Repopulate ``ctx.dir_by_report_id``/``report_id_by_dir`` from the index after
    :func:`grison.adapters.gw_report.sync_report_dirs` has (possibly) created a new
    report directory — the narrative/notes adapters resolve a report's directory
    purely through these maps, never the index directly."""
    ctx.dir_by_report_id = {}
    ctx.report_id_by_dir = {}
    for path, rec in index.records.items():
        if rec.kind is IndexKind.GW_REPORT:
            name = PurePosixPath(path).name
            ctx.dir_by_report_id[rec.id] = name
            ctx.report_id_by_dir[name] = rec.id


def _reports_json(reports: ReportsPhaseResult) -> str:
    summary = {
        "kinds": {
            k: {"counts": s.counts, "problem_paths": s.problem_paths}
            for k, s in reports.summaries.items()
        },
        "dirs": {
            "created": reports.dirs.created,
            "materialized": reports.dirs.materialized,
            "scope_failures": reports.dirs.scope_failures,
            "skipped": reports.dirs.skipped,
            "errors": reports.dirs.errors,
        },
        "snapshot_dir": str(reports.snapshot_dir) if reports.snapshot_dir else None,
        "exit_code": reports.exit_code,
    }
    return engine_events.render_json(reports.events, summary=summary)


def _print_reports_summary(
    reports: ReportsPhaseResult,
    *,
    dry_run: bool,
    verbose: bool = False,
) -> None:
    for line in engine_events.render_text_lines(reports.events, verbose=verbose):
        color = None
        if line.startswith(("collision", "invalid", "failed", "withheld")):
            color = typer.colors.RED
        elif line.startswith("skip"):
            color = typer.colors.YELLOW
        typer.secho(line, fg=color, dim=color is None)
    for kind, summary in reports.summaries.items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.counts.items()))
        typer.secho(f"reports ({kind}): {counts}", fg=typer.colors.GREEN)
        if summary.counts.get("withheld"):
            typer.secho(
                f"MASS-CHANGE GUARD tripped on {kind} — writes withheld.", fg=typer.colors.RED
            )
    d = reports.dirs
    if d.created:
        tense = "would create" if dry_run else "create"
        typer.echo(f"reports: {tense} {len(d.created)} report dir(s)")
    if d.materialized:
        typer.echo(f"reports: mirror {len(d.materialized)} .report.yml/project.md file(s)")
    for path, reason in d.skipped:
        typer.secho(f"skipped  {path}: {reason}", fg=typer.colors.YELLOW)
    if d.scope_failures:
        typer.secho(f"{len(d.scope_failures)} report(s) missing scope:", fg=typer.colors.RED)
        for msg in d.scope_failures:
            typer.echo(f"  ! {msg}")
    for e in d.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)
    # No per-phase "snapshot: ..." line here — see _print_findings_summary's comment.


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


@app.command()
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
    """Reverse a whole ``grison sync`` run's remote writes from its undo snapshot
    (``.grison/snapshots/``) — findings, report/note, and wiki, whichever this
    snapshot holds (one snapshot per sync RUN, not one per phase: ``grison sync``
    shares a single ``Snapshot`` across every phase it runs).

    Replays the snapshot's inverse operations through the adapters, newest write
    first (across every phase's kinds — a library push from the findings phase and
    an evidence upload from the report phase in the SAME run are undone by one
    ``grison undo``, newest first), each one guarded by the same pre-write re-fetch
    check ``grison sync`` itself uses — a record changed on the server since the
    snapshot was taken is reported, not silently overwritten. A snapshot may hold
    both Ghostwriter and BookStack kinds together, so this picks which remote(s) to
    contact, and which adapters to build, by looking at the kinds the snapshot
    actually recorded — never touching a remote the snapshot doesn't need. Owner-
    only: never run from an agent's own initiative (see the workspace's scaffolded
    ``.claude/settings.json`` deny-list).
    """
    root = find_workspace_root(Path.cwd())
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
            gw_client = stack.enter_context(_make_gw_client(creds))
            if kinds & findings_gw_kinds:
                gw_ctx = GWContext.build(gw_client, index)
                # the library adapter's ctx is the bare client (no report scoping
                # — see its class docstring), the other two take the GWContext.
                adapters[GwLibraryFindingAdapter.kind] = _BoundAdapter(
                    GwLibraryFindingAdapter(),
                    gw_client,
                )
                adapters[GwReportedFindingAdapter.kind] = _BoundAdapter(
                    GwReportedFindingAdapter(index=index, evidence_by_report={}),
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
            bs_client = stack.enter_context(_make_bs_client(creds))
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


_T = TypeVar("_T")


def _run_phase(name: str, fn: Callable[[], _T], phase_errors: list[str]) -> _T | None:
    """Run one sync phase (findings / reports / methodology) in isolation. An exception
    here is recorded in ``phase_errors`` and printed, but never propagated — the phases
    that follow still get to run, and the run exits nonzero at the end regardless."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — isolate this phase, subsequent phases still run
        msg = f"{name} sync failed: {e}"
        phase_errors.append(msg)
        typer.secho(msg, fg=typer.colors.RED)
        return None


@contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    """Serialize sync runs per workspace via an exclusive flock on .grison/lock."""
    lock_path = root / ".grison" / "lock"
    ensure_private_dir(lock_path.parent)
    fh = open_private(lock_path)
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.secho(
                "another grison sync is already running in this workspace (.grison/lock held)",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1) from None
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _git_commit_or_warn(root: Path, settings: Settings, message: str) -> None:
    """Commit under ``root`` if git driving is enabled — see :mod:`grison.gitdrive`.

    Silent no-op when the setting is off, or when the root isn't a git repo (detection
    is the feature). Any other git failure warns; it never fails the command, because
    grison's own outcome must not depend on git state.
    """
    if not settings.git_commit or not gitdrive.is_repo(root):
        return
    try:
        gitdrive.commit(root, message)
    except gitdrive.GitDriveError as e:
        typer.secho(f"git: {e}", fg=typer.colors.YELLOW)


def _scanner_label(summary: ParseSummary) -> str:
    return "+".join(sorted(summary.files_parsed)) or "(no files)"


def _findings_pull_push_counts(result: FindingsPhaseResult) -> tuple[int, int]:
    pulled = sum(
        s.counts.get("pull", 0) + s.counts.get("pull_new", 0) for s in result.summaries.values()
    )
    pushed = sum(
        s.counts.get("push", 0) + s.counts.get("create", 0) for s in result.summaries.values()
    )
    return pulled, pushed


def _git_sync_message(
    bad: bool,
    result: FindingsPhaseResult | None,
    rep: ReportsPhaseResult | None,
    wiki: WikiPhaseResult | None,
) -> str:
    """``bad`` is the same clean/failed signal that decides the process exit code — the
    commit message and the exit code must never disagree about whether this run was
    clean."""
    bits = []
    if bad:
        bits.append("with failures")
    if result is not None:
        pulled, pushed = _findings_pull_push_counts(result)
        bits.append(f"findings: pull {pulled} push {pushed}")
    if rep is not None:
        sections = rep.summaries.get(NarrativeSectionAdapter.kind)
        pulled = sections.counts.get("pull", 0) if sections else 0
        pushed = sections.counts.get("push", 0) if sections else 0
        bits.append(f"reports: pull {pulled} push {pushed}")
    if wiki is not None:
        counts = wiki.summaries.get(BsPageAdapter.kind)
        pulled = counts.counts.get("pull", 0) if counts else 0
        pushed = counts.counts.get("push", 0) if counts else 0
        bits.append(f"wiki: pull {pulled} push {pushed}")
    return f"grison: sync ({'; '.join(bits)})" if bits else "grison: sync"


def _print_parse_summary(summary: ParseSummary, out_dir: Path, *, dry_run: bool) -> None:
    n_files = sum(summary.files_parsed.values())
    by_scanner = ", ".join(f"{k}: {v}" for k, v in sorted(summary.files_parsed.items()))
    typer.secho(
        f"Parsed {len(summary.findings)} finding(s) from {n_files} file(s)"
        + (f" ({by_scanner})" if by_scanner else ""),
        fg=typer.colors.GREEN,
    )

    sink = summary.sink
    if sink is not None:
        verb = "Would write" if dry_run else "Wrote"
        typer.echo(f"{verb} {len(sink.written)} → {out_dir}  ({len(sink.unchanged)} unchanged)")

    for path, reason in summary.skipped_files:
        typer.secho(f"skipped  {path.name}: {reason}", fg=typer.colors.YELLOW)

    if summary.warnings:
        typer.secho(f"{len(summary.warnings)} warning(s):", fg=typer.colors.YELLOW)
        for w in summary.warnings:
            typer.echo(f"  - {w}")

    if summary.errors:
        typer.secho(f"{len(summary.errors)} finding(s) failed validation:", fg=typer.colors.RED)
        for e in summary.errors:
            typer.echo(f"  - {e}")


# --- self-contained-workspace scaffolding (brief D11/D12/D13) ----------------------
#
# Everything an agent reads in a workspace (CLAUDE.md, .claude/settings.json,
# .grison/SPEC.md, .grison/templates/) is generated from the same definitions
# grison.validator enforces — see grison/scaffold/ for the generators and
# grison/remote/bootstrap.py for where this runs automatically on every sync/parse.


@app.command()
@_guarded
def scaffold(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Recompute the idempotent parts (CLAUDE.md, "
            ".claude/settings.json, the root .gitignore, the git pre-commit hook) "
            "from scratch instead of only adding what's missing. Never touches "
            ".grison/templates/ (an operator may have customized them) or a "
            "hand-edited CLAUDE.md.",
        ),
    ] = False,
) -> None:
    """(Re)generate every scaffolded file: ``.grison/SPEC.md``, ``.grison/templates/``,
    ``.grison/terms.txt``, ``CLAUDE.md``, ``.claude/settings.json``, the root
    ``.gitignore``'s collision-sidecar entry, and (in a git repo) the ``pre-commit``
    hook. Runs automatically on every ``grison sync``/``grison parse`` too — this
    command is for regenerating on demand, e.g. after upgrading grison.
    """
    from grison.scaffold import scaffold_workspace as _scaffold_workspace

    root = Path.cwd()
    bootstrap_tree(root)
    ensure_private_dir(root / ".grison")
    settings = load_settings(root)
    result = _scaffold_workspace(root, force=force, settings=settings)

    if result.spec_written:
        typer.echo("wrote .grison/SPEC.md")
    for name in result.templates_written:
        typer.echo(f"wrote .grison/templates/{name}")
    if result.terms_written:
        typer.echo("wrote .grison/terms.txt")
    if result.settings_updated:
        typer.echo("updated .claude/settings.json")
    typer.echo(f"CLAUDE.md: {result.claude_md_status}")
    pc = result.precommit
    if pc is not None:
        typer.secho(
            f"pre-commit hook: {pc.reason}", fg=typer.colors.YELLOW if not pc.installed else None
        )
        if pc.instructions:
            typer.echo(pc.instructions)


hook_app = typer.Typer(help="Hooks grison scaffolds into .claude/settings.json.")
app.add_typer(hook_app, name="hook")


@hook_app.command("post-edit")
def hook_post_edit() -> None:
    """The PostToolUse hook body (see grison/scaffold/hook.py) — reads the tool-call
    JSON from stdin, validates only the edited file, and prints feedback for the
    agent. Always exits 0: a PostToolUse hook cannot block a tool call that already
    ran, so this only ever informs, never fails.
    """
    from grison.scaffold.hook import main as _hook_main

    raise typer.Exit(code=_hook_main())


def main() -> None:
    """Console-script entry point (``grison``)."""
    app()


if __name__ == "__main__":
    main()
