"""``grison sync`` — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from grison.cli import app, clients
from grison.cli.gitdriving import _git_commit_or_warn, _git_sync_message
from grison.cli.guards import (
    _guarded,
    _is_bootstrapped,
    _print_would_bootstrap,
    _refuse_if_format_mismatch,
    _refuse_if_workspace_rules_fail,
    _workspace_lock,
)
from grison.cli.payloads import (
    _findings_last_sync_summary,
    _findings_payload,
    _phase_error,
    _phase_payload_or_error,
    _record_phase_last_sync,
    _reports_last_sync_summary,
    _reports_payload,
    _wiki_last_sync_summary,
    _wiki_payload,
)
from grison.cli.phases import common as phase_common
from grison.cli.phases import findings as findings_phase
from grison.cli.phases import reports as reports_phase
from grison.cli.phases import wiki as wiki_phase
from grison.cli.phases.wiki import WikiPhaseResult
from grison.cli.render import _print_findings_summary, _print_reports_summary, _print_wiki_summary
from grison.engine.undo import Snapshot
from grison.remote.bootstrap import BootstrapResult, bootstrap_workspace
from grison.remote.compat import SchemaCompatibilityError, check_ghostwriter_compatibility
from grison.remote.creds import MissingCreds, load_settings
from grison.remote.creds import load as load_creds
from grison.workspace import bootstrap_tree


@app.command(help="Push local edits, pull remote changes, flag anything changed on both sides.")
@_guarded
def sync(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview the sync plan, write nothing.",
        ),
    ] = False,
    force_local: Annotated[
        Path | None,
        typer.Option(
            "--force-local",
            help="The local side wins for this path.",
        ),
    ] = None,
    force_remote: Annotated[
        Path | None,
        typer.Option(
            "--force-remote",
            help="The remote side wins for this path.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="JSON output.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Also list informational skips (drafts, templates).",
        ),
    ] = False,
    allow_mass_change: Annotated[
        bool,
        typer.Option(
            "--allow-mass-change",
            help="Let this run through the mass-change guard (bulk imports).",
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
    _refuse_if_format_mismatch(root)  # D13 (item 10) — before any bootstrap/work
    bootstrapped = _is_bootstrapped(root)
    if dry_run and not bootstrapped:
        # item 2, fix-fin1: a real (non-dry) first sync bootstraps a fresh
        # directory from scratch (~11 files) — `--dry-run` must never do that
        # write for real, and there is nothing else to preview yet either, so
        # this reports what a real run would create and stops.
        _print_would_bootstrap(root)
        raise typer.Exit(code=2)
    if bootstrapped:
        _refuse_if_workspace_rules_fail(root)  # item 1 — before the self-heal below
    if dry_run:
        from grison.scaffold import ScaffoldResult

        # bootstrap_tree only ever creates plain, empty directories (never a
        # file), so it never shows up in a before/after byte comparison — the
        # same reasoning `status --remote`'s own dry classify already relies on
        # (see its comment below). Everything else `bootstrap_workspace` would
        # write for real (env template, manifest.yml, scaffolded files, self-
        # healing chmod passes) is skipped entirely under `--dry-run`.
        bootstrap_tree(root)
        boot = BootstrapResult(
            created_dirs=[],
            env_created=False,
            env_path=root / ".grison" / "env",
            claude_md_created=False,
            scaffold=ScaffoldResult(),
        )
    else:
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
        # ENGINE.md §10: missing/bad credentials is "could not run" — exit 2, same
        # class as no-workspace/incompatible-server/lock-held (item 6, fix-fin1;
        # this used to exit 1, the "ran but needs attention" class).
        raise typer.Exit(code=2) from None

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
        with clients._make_gw_client(creds) as client:
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
            rep_and_evidence = phase_common._run_phase(
                "report",
                lambda: reports_phase._run_reports_phase(
                    root,
                    client,
                    dry_run=dry_run,
                    force_local=fl,
                    force_remote=fr,
                    allow_mass_change=allow_mass_change,
                    snapshot=snapshot,
                    quiet=json_output,
                ),
                phase_errors,
            )
            rep, evidence_by_report = (
                rep_and_evidence if rep_and_evidence is not None else (None, {})
            )
            _checkpoint_snapshot()  # durable now, even if findings/wiki never run
            result = phase_common._run_phase(
                "findings",
                lambda: findings_phase._run_findings_phase(
                    root,
                    client,
                    dry_run=dry_run,
                    force_local=fl,
                    force_remote=fr,
                    allow_mass_change=allow_mass_change,
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
                with clients._make_bs_client(creds) as bs:
                    return wiki_phase._run_wiki_phase(
                        root,
                        bs,
                        dry_run=dry_run,
                        force_local=fl,
                        force_remote=fr,
                        allow_mass_change=allow_mass_change,
                        snapshot=snapshot,
                        quiet=json_output,
                    )

            wiki = phase_common._run_phase("wiki", _do_wiki, phase_errors)
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

        if json_output:
            # item 8, fix-fin1: ONE combined JSON document for the whole run —
            # before this fix, `--json` printed one JSON-Lines blob per phase
            # (findings, then report, then wiki), each with its own repeated
            # `snapshot_dir`/`exit_code` — three separate streams a consumer had
            # to know to concatenate-and-parse rather than one parse() call.
            # `snapshot`/the overall `exit_code` are hoisted here, once; each
            # phase's own value is `null` if it never even ran (e.g. no BookStack
            # credentials configured), `{"error": "..."}` if it raised before
            # producing a result, or its normal `{"events": [...], "summary":
            # {...}}` otherwise.
            typer.echo(
                json.dumps(
                    {
                        "report": _phase_payload_or_error(
                            rep, _reports_payload, phase_errors, "report"
                        ),
                        "findings": _phase_payload_or_error(
                            result, _findings_payload, phase_errors, "findings"
                        ),
                        "wiki": _phase_payload_or_error(wiki, _wiki_payload, phase_errors, "wiki"),
                        "snapshot": str(snapshot_dir) if snapshot_dir is not None else None,
                        "exit_code": 1 if bad else 0,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            if result is not None:
                _print_findings_summary(result, dry_run=dry_run, verbose=verbose)
            if rep is not None:
                _print_reports_summary(rep, dry_run=dry_run, verbose=verbose)
            if wiki is not None:
                _print_wiki_summary(wiki, dry_run=dry_run, verbose=verbose)
            if snapshot_dir is not None:
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
