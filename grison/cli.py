"""grison CLI — three verbs: ``parse``, ``status``, ``sync``.

The path names the backend (``findings/`` ⇄ Ghostwriter, ``methodology/`` ⇄
BookStack); location decides identity; the first ``sync`` bootstraps the workspace.
``parse`` and ``status`` are offline; ``sync`` reconciles findings with Ghostwriter
and methodology with BookStack (push/pull/collision derived per record).
"""

from __future__ import annotations

import fcntl
import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, TypeVar

import typer

from grison import gitdrive
from grison.adapters import bs_structure
from grison.adapters._bs_common import build_context
from grison.adapters.bs_pages import BsPageAdapter
from grison.engine import events as engine_events
from grison.engine.adapter import UndoAdapter
from grison.engine.apply import RunOptions
from grison.engine.apply import run as engine_run
from grison.engine.model import PROBLEM_OUTCOMES, Event, KindSummary, Plan
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot
from grison.engine.undo import list_snapshots as engine_list_snapshots
from grison.engine.undo import replay as engine_undo_replay
from grison.errors import GrisonError
from grison.fsio import ensure_private_dir, open_private
from grison.index import Index
from grison.model import FindingType
from grison.remote.bookstack import BookStackClient
from grison.remote.bootstrap import bootstrap_workspace
from grison.remote.creds import Creds, MissingCreds, Settings, load_settings
from grison.remote.creds import load as load_creds
from grison.remote.ghostwriter import GhostwriterClient
from grison.remote.reports import ReportResult, sync_reports
from grison.remote.sync import SyncResult
from grison.remote.sync import sync as run_sync
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
        typer.Option("--version", callback=_print_version, is_eager=True,
                     help="Print the version and exit."),
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
        bootstrap_tree(root)  # the binary scaffolds; no init
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
        typer.Option("--remote", help="Also contact BookStack and classify (dry-run, no writes) "
                     "what a sync would do — offline otherwise (index + private state + the "
                     "validator only)."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Whole-workspace overview: counts per area, only non-clean paths listed.

    Offline by default (the index, private state, and ``grison validate``'s own
    checks — no credentials, no network). ``--remote`` additionally contacts
    BookStack and runs the engine's classify step in dry-run mode for the wiki, so
    the report also shows what the next ``grison sync`` would actually do there.
    Findings/reports are not yet engine-managed (a later step); this command reports
    plain, validation-free file counts for them and says so.

    Exit code: 0 clean, 1 something needs attention (matches ``grison validate``'s
    own policy — see ``grison sync``'s result/exit-code policy in ENGINE.md §10),
    2 could not run (no workspace).
    """
    try:
        root = find_workspace_root(Path.cwd())
    except GrisonError as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None

    failures = validate_workspace(root)
    state = StateStore(root)
    last_sync = state.load_last_sync()

    findings_lib = root / "findings" / "library"
    findings_reports = root / "findings" / "reports"
    lib_count = len(list(findings_lib.glob("*.md"))) if findings_lib.is_dir() else 0
    report_dirs = sorted(p for p in findings_reports.iterdir() if p.is_dir()) \
        if findings_reports.is_dir() else []

    index = Index.load(root)
    page_paths = sorted(p for p, r in index.records.items() if r.kind.value == "bs.page")
    wiki_failures = [f for f in failures if f.path.startswith("methodology")]
    wiki_problem_paths = sorted({f.path for f in wiki_failures})

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
                        root, ctx, dry_index, state, Snapshot(), dry_run=True,
                    )
                    _plans, _events, remote_summary = engine_run(
                        root, ctx, BsPageAdapter(), index=dry_index, state=state,
                        snapshot=Snapshot(), failures=wiki_failures,
                        options=RunOptions(dry_run=True),
                    )
            except GrisonError as e:
                remote_error = str(e)
        else:
            remote_error = "BookStack credentials not configured"

    problems = bool(wiki_failures) or (
        remote_summary is not None
        and any(k in remote_summary.counts for k in ("collision", "invalid", "failed", "withheld"))
    )

    if json_output:
        payload = {
            "findings": {"managed": False, "library_files": lib_count,
                        "report_dirs": len(report_dirs)},
            "methodology": {
                "managed": True, "pages_tracked": len(page_paths),
                "invalid": len(wiki_problem_paths), "problem_paths": wiki_problem_paths,
            },
            "remote": (
                {"counts": remote_summary.counts, "problem_paths": remote_summary.problem_paths}
                if remote_summary is not None else remote_error
            ),
            "last_sync": last_sync,
        }
        typer.echo(json.dumps(payload, indent=2))
        if problems:
            raise typer.Exit(code=1)
        return

    typer.echo(
        f"findings: {lib_count} library file(s), {len(report_dirs)} report dir(s) "
        "— not yet engine-managed"
    )
    tag = "clean" if not wiki_problem_paths else f"{len(wiki_problem_paths)} invalid"
    typer.echo(f"methodology: {len(page_paths)} page(s) tracked ({tag})")
    for p in wiki_problem_paths:
        typer.secho(f"  ! {p}", fg=typer.colors.RED)
    if remote_summary is not None:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(remote_summary.counts.items()))
        typer.echo(f"remote (--remote, dry-run): {counts}")
        for p in remote_summary.problem_paths:
            typer.secho(f"  ! {p}", fg=typer.colors.RED)
    elif remote_error is not None:
        typer.secho(f"remote: {remote_error}", fg=typer.colors.YELLOW)
    if last_sync is not None:
        typer.echo(f"last sync: {last_sync.get('at', '?')}")
    else:
        typer.echo("last sync: never")

    if problems:
        raise typer.Exit(code=1)


@app.command()
@_guarded
def validate(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Only these files/dirs (plus the cross-file rules they touch) — "
                       "default: the whole workspace."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a stable, machine-readable JSON array instead."),
    ] = False,
    deleted_ok: Annotated[
        bool,
        typer.Option("--deleted-ok", help="A given path that no longer exists still validates "
                     "its containing directory's cross-file rules (for a post-edit hook running "
                     "after a delete), instead of failing with 'no such path'."),
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
        typer.echo(json.dumps(
            [
                {
                    "rule_id": f.rule_id, "path": f.path, "line": f.line,
                    "message": f.message, "fix": f.fix,
                }
                for f in failures
            ],
            indent=2,
        ))
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
        typer.Option("--json", help="Emit the wiki phase's events as JSON lines (one object "
                     "per event plus a final summary object). The findings/report phases are "
                     "not yet engine-managed and keep their existing text output either way."),
    ] = False,
) -> None:
    """Reconcile the workspace with Ghostwriter + BookStack — push/pull/collision
    derived per record.

    Bootstraps on first run. Direction isn't chosen: a locally-edited record pushes, a
    remote-changed one pulls, and a record changed on both sides is surfaced (never
    overwritten). Every remote write is snapshot-backed. ``--force-local``/
    ``--force-remote`` accept a path under ``methodology/`` (routed to the wiki engine)
    or under ``findings/`` (routed to the older findings/report phases).
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
    with _workspace_lock(root):  # one sync at a time per workspace (GW has no compare-and-swap)
        if not dry_run:  # checkpoint whatever was dirty before we touch anything
            _git_commit_or_warn(root, settings, "grison: pre-sync checkpoint")
        with _make_gw_client(creds) as client:
            result = _run_phase(
                "findings",
                lambda: run_sync(
                    root, client, dry_run=dry_run, force_local=fl, force_remote=fr,
                    on_event=lambda msg: typer.secho(msg, dim=True),
                ),
                phase_errors,
            )
            rep = _run_phase(
                "report",
                lambda: sync_reports(
                    root, client, dry_run=dry_run, force_local=fl, force_remote=fr,
                    on_event=lambda msg: typer.secho(msg, dim=True),
                ),
                phase_errors,
            )
        if result is not None:
            _print_sync_summary(result, dry_run=dry_run)
        if rep is not None:
            _print_report_summary(rep, dry_run=dry_run)
        bad = bool(phase_errors)
        if result is not None:
            bad = bad or bool(
                result.collisions or result.invalid or result.corrupt
                or result.mass_change_blocked or result.errors
            )
        if rep is not None:
            bad = bad or bool(
                rep.collisions or rep.mass_change_blocked or rep.errors or rep.scope_failures
            )

        if creds.bs_url and creds.bs_token_id and creds.bs_token_secret:
            def _do_wiki() -> WikiPhaseResult:
                with _make_bs_client(creds) as bs:
                    return _run_wiki_phase(
                        root, bs, dry_run=dry_run, force_local=fl, force_remote=fr,
                    )

            wiki = _run_phase("wiki", _do_wiki, phase_errors)
            if wiki is not None:
                if json_output:
                    typer.echo(_wiki_json(wiki))
                else:
                    _print_wiki_summary(wiki, dry_run=dry_run)
                bad = bad or wiki.exit_code != 0
        bad = bad or bool(phase_errors)  # catches a wiki-phase failure too

        if not dry_run:  # capturing state is the point, even (especially) after failures
            _git_commit_or_warn(root, settings, _git_sync_message(bad, result, rep, wiki))
            if wiki is not None:
                _save_last_sync(root, wiki, bad=bad)

    if bad:
        raise typer.Exit(code=1)


def _save_last_sync(root: Path, wiki: WikiPhaseResult, *, bad: bool) -> None:
    """``.grison/state/last-sync.json`` (ENGINE.md 'State') — read by ``grison
    status``. Wiki-only today (the findings/report phases aren't engine-managed yet);
    a later step folds their own summaries in here too."""
    payload = {
        "at": datetime.now(UTC).isoformat(),
        "ok": not bad,
        "kinds": {k: {"counts": s.counts, "problem_paths": s.problem_paths}
                 for k, s in wiki.summaries.items()},
        "snapshot_dir": str(wiki.snapshot_dir) if wiki.snapshot_dir else None,
    }
    StateStore(root).save_last_sync(payload)


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
        if any(p.outcome in PROBLEM_OUTCOMES for p in self.plans) or self.structure.errors:
            return 1
        return 0


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
    root: Path, client: BookStackClient, *, dry_run: bool, force_local: set[Path],
    force_remote: set[Path],
) -> WikiPhaseResult:
    """The wiki phase: BookStack structure (books/chapters/shelves — creates any new
    local book/chapter directory, then regenerates the read-only mirrors), then pages
    through :mod:`grison.engine.apply`. The validation gate is scoped to
    ``methodology/`` here (task step 1's scope parameter — findings/reports are still
    format v1 and would fail v2 validation wholesale); pulls are never blocked by it,
    only pushes/creates/deletes (see ``grison.engine.apply``'s own gate)."""
    index = Index.load(root)
    state = StateStore(root)
    snapshot = Snapshot()
    ctx = build_context(client, state)

    all_failures = validate_workspace(root, paths=[root / "methodology"])
    wiki_failures = [f for f in all_failures if f.path.startswith("methodology")]

    structure = bs_structure.sync_structure(
        root, ctx, index, state, snapshot, dry_run=dry_run,
        on_event=lambda msg: typer.secho(msg, dim=True),
    )

    options = RunOptions(
        dry_run=dry_run,
        force_local=_wiki_relative_force_set(root, force_local),
        force_remote=_wiki_relative_force_set(root, force_remote),
    )
    plans, events, summary = engine_run(
        root, ctx, BsPageAdapter(), index=index, state=state, snapshot=snapshot,
        failures=wiki_failures, options=options,
    )

    snapshot_dir: Path | None = None
    if not dry_run:
        index.save()
        if not snapshot.empty:
            snapshot_dir = snapshot.persist(root)

    return WikiPhaseResult(
        plans=plans, events=events, summaries={BsPageAdapter.kind: summary},
        structure=structure, snapshot_dir=snapshot_dir,
    )


def _wiki_json(wiki: WikiPhaseResult) -> str:
    summary = {
        "kinds": {k: {"counts": s.counts, "problem_paths": s.problem_paths}
                 for k, s in wiki.summaries.items()},
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


def _print_wiki_summary(wiki: WikiPhaseResult, *, dry_run: bool) -> None:
    for line in engine_events.render_text_lines(wiki.events):
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
    if wiki.snapshot_dir:
        typer.echo(f"snapshot: {wiki.snapshot_dir}")


@app.command()
@_guarded
def undo(
    snapshot: Annotated[
        str | None,
        typer.Argument(help="Snapshot name (default: the most recent one)."),
    ] = None,
    list_: Annotated[
        bool, typer.Option("--list", help="List available snapshots, newest first, and exit."),
    ] = False,
) -> None:
    """Reverse a sync's wiki writes from its undo snapshot (``.grison/snapshots/``).

    Replays the snapshot's inverse operations through the adapters, newest write
    first, each one guarded by the same pre-write re-fetch check ``grison sync``
    itself uses — a record changed on the server since the snapshot was taken is
    reported, not silently overwritten. Owner-only: never run from an agent's own
    initiative (see the workspace's scaffolded ``.claude/settings.json`` deny-list).
    """
    root = find_workspace_root(Path.cwd())
    names = engine_list_snapshots(root)
    if list_:
        if not names:
            typer.echo("no snapshots")
        for n in names:
            typer.echo(n)
        return
    if not names:
        typer.secho("no snapshots to undo", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    target = snapshot or names[0]
    if target not in names:
        typer.secho(f"error: no such snapshot {target!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    creds = load_creds(root)
    creds.require_bookstack()
    with _make_bs_client(creds) as client:
        ctx = build_context(client, StateStore(root))
        adapters: dict[str, UndoAdapter] = {
            BsPageAdapter.kind: BsPageAdapter(),
            bs_structure.BookUndoAdapter.kind: bs_structure.BookUndoAdapter(client),
            bs_structure.ChapterUndoAdapter.kind: bs_structure.ChapterUndoAdapter(client),
        }
        problems = engine_undo_replay(
            root, target, ctx=ctx, adapters=adapters,
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


def _git_sync_message(
    bad: bool,
    result: SyncResult | None,
    rep: ReportResult | None,
    wiki: WikiPhaseResult | None,
) -> str:
    """``bad`` is the same clean/failed signal that decides the process exit code — the
    commit message and the exit code must never disagree about whether this run was
    clean."""
    bits = []
    if bad:
        bits.append("with failures")
    if result is not None:
        bits.append(f"findings: pull {len(result.pulled)} push {len(result.pushed)}")
    if rep is not None:
        bits.append(f"reports: pull {len(rep.pulled)} push {len(rep.pushed)}")
    if wiki is not None:
        counts = wiki.summaries.get(BsPageAdapter.kind)
        pulled = counts.counts.get("pull", 0) if counts else 0
        pushed = counts.counts.get("push", 0) if counts else 0
        bits.append(f"wiki: pull {pulled} push {pushed}")
    return f"grison: sync ({'; '.join(bits)})" if bits else "grison: sync"


def _print_report_summary(rep: ReportResult, *, dry_run: bool) -> None:
    tense = "would " if dry_run else ""
    typer.secho(
        f"reports: {tense}pull {len(rep.pulled)}, {tense}push {len(rep.pushed)}  "
        f"({len(rep.unchanged)} clean, {len(rep.repaired)} repaired)",
        fg=typer.colors.GREEN,
    )
    if rep.notes_pushed:
        typer.echo(f"notes: {tense}push {len(rep.notes_pushed)}")
    if rep.snapshot_dir:
        typer.echo(f"snapshot: {rep.snapshot_dir}")
    if rep.mass_change_blocked:
        typer.secho("MASS-CHANGE GUARD tripped on reports — pushes withheld.", fg=typer.colors.RED)
    if rep.collisions:
        typer.secho(
            f"{len(rep.collisions)} report-section collision(s) — hand-merge then --force-*:",
            fg=typer.colors.RED,
        )
        for p in rep.collisions:
            typer.echo(f"  ! {p}")
    if rep.scope_failures:
        typer.secho(f"{len(rep.scope_failures)} report(s) missing scope:", fg=typer.colors.RED)
        for msg in rep.scope_failures:
            typer.echo(f"  ! {msg}")
    for p, reason in rep.skipped:
        typer.secho(f"skipped  {p}: {reason}", fg=typer.colors.YELLOW)
    for e in rep.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)
    for w in rep.warnings:
        typer.secho(f"  warning: {w}", dim=True)


def _print_sync_summary(result: SyncResult, *, dry_run: bool) -> None:
    tense = "would " if dry_run else ""
    ev = ""
    if result.evidence_up or result.evidence_down or result.evidence_deleted:
        ev = f"  [evidence ↑{result.evidence_up} ↓{result.evidence_down}"
        if result.evidence_deleted:
            ev += f" ✕{result.evidence_deleted}"
        ev += "]"
    typer.secho(
        f"{tense}pull {len(result.pulled)}, {tense}push {len(result.pushed)}, "
        f"{tense}insert {len(result.inserted)}  ({len(result.unchanged)} clean, "
        f"{len(result.repaired)} repaired){ev}",
        fg=typer.colors.GREEN,
    )
    if result.snapshot_dir:
        typer.echo(f"snapshot: {result.snapshot_dir}")
    if result.mass_change_blocked:
        typer.secho(
            "MASS-CHANGE GUARD tripped — remote writes withheld. Re-run a narrower path "
            "or confirm with a targeted sync.",
            fg=typer.colors.RED,
        )
    if result.collisions:
        typer.secho(
            f"{len(result.collisions)} collision(s) — hand-merge then --force-local/-remote:",
            fg=typer.colors.RED,
        )
        for p in result.collisions:
            typer.echo(f"  ! {p}  (remote at {p.with_suffix('.remote.md').name})")
    if result.invalid:
        typer.secho(f"{len(result.invalid)} broken link(s) (id set, no sync base) — re-link with "
                    "--force-remote/--force-local:", fg=typer.colors.RED)
        for p in result.invalid:
            typer.echo(f"  ? {p}")
    if result.corrupt:
        typer.secho(
            f"{len(result.corrupt)} corrupt local file(s) — fix and re-sync:", fg=typer.colors.RED
        )
        for p, msg in result.corrupt:
            typer.echo(f"  ✕ {p}: {msg}")
    for p, reason in result.skipped:
        typer.secho(f"skipped  {p}: {reason}", fg=typer.colors.YELLOW)
    for e in result.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)
    for w in result.warnings:
        typer.secho(f"  warning: {w}", dim=True)


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


def main() -> None:
    """Console-script entry point (``grison``)."""
    app()


if __name__ == "__main__":
    main()
