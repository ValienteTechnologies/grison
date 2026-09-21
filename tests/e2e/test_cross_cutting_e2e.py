"""End-to-end cross-cutting scenarios that span all three sync phases, or sit outside
the reconcile engine entirely: exit-code policy, the workspace lock, opt-in git
driving (``GRISON_GIT=commit``), and first-run bootstrap.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import tree_snapshot

REPORT_SCOPES = [
    {
        "name": "Internal range",
        "scope": "10.0.0.0/24",
        "description": "",
        "disallowed": False,
        "requiresCaution": False,
    }
]


# ---------------------------------------------------------------------------
# Exit-code policy per phase
#
# Transcribed from grison/cli.py's sync() (read, not reproduced by import — this
# test would need updating, not silently pass, if the predicate changes):
#
#     bad = bool(phase_errors)
#     if result is not None:  (findings)
#         bad |= bool(result.collisions or result.invalid or result.corrupt
#                      or result.mass_change_blocked or result.errors)
#     if rep is not None:  (reports)
#         bad |= bool(rep.collisions or rep.mass_change_blocked or rep.errors
#                      or rep.scope_failures)
#     if m is not None:  (methodology)
#         bad |= bool(m.collisions or m.invalid or m.drift or m.artifacts
#                      or m.mass_change_blocked or m.errors)
#
# Notably: `skipped` and `warnings` never flip `bad` for ANY phase — a lone
# wysiwyg-page skip or an orphan-record skip is not itself a failure. Notably too,
# a methodology `artifacts` hit flips `bad` even when the artifact was pre-existing
# and the push was NOT refused (see test_methodology_e2e.py's grandfathering test) —
# the corruption is merely *surfaced*, but its presence alone still taints the run.
#
# Today, `grison sync`'s overall exit code is 1 in EVERY case below regardless of
# which case fires, because the findings phase fails unconditionally before any
# record is even looked at (the evidence-query production break — see
# test_findings_e2e.py) and phase_errors is always non-empty. This is exactly the
# fragmentation D6 ("one result/exit-code policy") replaces: today, three different
# `bad` predicates happen to agree only because one of them is permanently tripped.
# What each case below actually proves is that the phase's own summary line reports
# the case honestly, not that the exit code discriminates between them (it can't,
# today) — see each case's ``marker`` string in the assertion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_name",
    ["methodology_collision", "reports_scope_failure", "methodology_mass_change"],
)
def test_exit_code_is_always_1_today_but_each_failure_class_reports_honestly(
    run_grison,
    gw_server,
    bs_server,
    case_name,
) -> None:
    if case_name == "methodology_collision":
        book = bs_server.store.seed_book(name="Playbook")
        page = bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")
        run_grison("sync")
        path = Path.cwd() / "methodology" / "library" / "playbook" / "notes.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nlocal change\n", encoding="utf-8")
        # a real BookStack write bumps updated_at/revision_count too (see
        # BSStore.edit_page's own docstring for why a bare dict mutation would not
        # be detected by the skip-detail-fetch fast path)
        bs_server.store.edit_page(page["id"], markdown="# N\n\nremote change")
        marker = "collision"
    elif case_name == "reports_scope_failure":
        gw_server.store.seed_report(
            id=7,
            title="R",
            extraFields={"executive_summary": "<p>s</p>"},
            project={"codename": "OP-X", "scopes": []},
        )
        marker = "no scope defined"
    else:
        # the engine's change guard (ENGINE.md) covers bulk PULLs too, unlike the old
        # module (which only ever guarded push/create) — so 6 brand-new remote pages
        # pulled in ONE sync would itself trip the guard and withhold the pull. Seed
        # and pull in two batches (5, then 1 more) so every page actually lands on
        # disk before the test edits all 6 locally to trip the PUSH side instead.
        book = bs_server.store.seed_book(name="Playbook")
        for i in range(5):
            bs_server.store.seed_page(book_id=book["id"], name=f"Page {i}", markdown=f"# {i}")
        run_grison("sync")
        bs_server.store.seed_page(book_id=book["id"], name="Page 5", markdown="# 5")
        run_grison("sync")
        for i in range(6):
            p = Path.cwd() / "methodology" / "library" / "playbook" / f"page-{i}.md"
            p.write_text(p.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        marker = "MASS-CHANGE GUARD"

    result = run_grison("sync")

    assert marker in result.output
    assert result.exit_code == 1  # always 1 today — see the module-level note above


def test_a_lone_skip_does_not_by_itself_change_the_exit_code(run_grison, bs_server) -> None:
    """A skip (wysiwyg page, orphaned record, …) is not, on its own, a failure — SKIP
    is not in ``grison.engine.model.PROBLEM_OUTCOMES`` (tests changed on purpose: the
    wiki phase's own summary line is now the engine's ``wiki (bs.page): ...`` counts
    line, not the old ``methodology: pull/push/create (... clean, ... repaired)`` — see
    ``grison.cli._print_wiki_summary``)."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(
        book_id=book["id"],
        name="WYSIWYG",
        editor="wysiwyg",
        markdown="",
        raw_html="<p>rich</p>",
    )

    # a wysiwyg page grison has never managed is INFO-severity (nothing lost, nothing
    # to do) — hidden from plain text by design (VetoSeverity); --verbose to see the
    # line and prove it's still just a skip, never a "problem" outcome.
    result = run_grison("sync", "--verbose")

    assert "wysiwyg" in result.output.lower()
    # the wiki phase's own summary shows nothing blocked — a lone skip, no collision/
    # invalid/failed/withheld; only the ever-present findings-phase break (the
    # evidence-query production bug tracked in test_findings_e2e.py) makes the
    # overall exit code 1 (see the module note above).
    assert "wiki (bs.page): skip 1" in result.output


# ---------------------------------------------------------------------------
# Workspace lock
# ---------------------------------------------------------------------------


def test_second_concurrent_sync_refuses_while_the_lock_is_held(run_grison, workspace) -> None:
    """ENGINE.md §10: refusing before the first fetch is "could not run" (exit 2),
    not "ran but needs attention" (exit 1) — tests changed on purpose: this used to
    assert exit 1. Nothing is written either (the refusal happens before
    `sync`'s pre-sync git checkpoint or any phase runs)."""
    from tests.conftest import tree_snapshot

    run_grison("sync")  # one real (unlocked) sync first, so bootstrap scaffolding
    # (CLAUDE.md, .claude/settings.json, …) has already happened and isn't mistaken
    # for something the REFUSED sync below wrote.

    lock_dir = workspace / ".grison"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / "lock").open("w", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    exclude = frozenset({".grison/state/mirrors.json"})
    try:
        before = tree_snapshot(workspace, exclude=exclude)
        result = run_grison("sync")
        assert result.exit_code == 2
        assert "another grison sync is already running" in result.output
        assert tree_snapshot(workspace, exclude=exclude) == before
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


# ---------------------------------------------------------------------------
# Opt-in git driving (GRISON_GIT=commit)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _git("init", cwd=path)
    _git("config", "user.name", "grison-test", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)


def _log_subjects(path: Path) -> list[str]:
    r = _git("log", "--pretty=%s", cwd=path)
    return r.stdout.splitlines() if r.returncode == 0 else []


def test_git_commit_checkpoints_and_commits_after_a_real_sync(
    run_grison,
    workspace,
    bs_server,
    monkeypatch,
) -> None:
    _init_repo(workspace)
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync")

    subjects = _log_subjects(workspace)
    assert any(s == "grison: pre-sync checkpoint" for s in subjects)
    assert any(s.startswith("grison: sync") for s in subjects)
    assert _git("status", "--porcelain", cwd=workspace).stdout.strip() == ""  # fully committed


def test_git_commit_dry_run_commits_nothing(run_grison, workspace, bs_server, monkeypatch) -> None:
    _init_repo(workspace)
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync", "--dry-run")

    r = _git("rev-list", "--all", "--count", cwd=workspace)
    assert r.stdout.strip() == "0"  # not a single commit was made


def test_git_staging_is_scoped_to_the_workspace_root(
    run_grison,
    workspace,
    bs_server,
    monkeypatch,
) -> None:
    """The workspace may be a subdirectory of a larger repo — staging must never
    reach outside it (``git add -A -- .`` with ``-C <workspace root>``)."""
    outer = workspace.parent
    _init_repo(outer)
    (outer / "unrelated-outer-file.txt").write_text("do not touch\n", encoding="utf-8")
    monkeypatch.setenv("GRISON_GIT", "commit")
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    run_grison("sync")

    status = _git("status", "--porcelain", cwd=outer).stdout
    assert "unrelated-outer-file.txt" in status  # still untracked — never staged
    subjects = _log_subjects(outer)
    assert any(s.startswith("grison: sync") for s in subjects)  # the workspace's own commit landed


# ---------------------------------------------------------------------------
# CLAUDE.md scaffold + first-run bootstrap
# ---------------------------------------------------------------------------


def test_claude_md_scaffolded_on_first_bootstrap_and_never_clobbered(run_grison, bs_server) -> None:
    claude_md = Path.cwd() / "CLAUDE.md"
    assert not claude_md.exists()

    run_grison("sync")

    assert claude_md.exists()
    assert "grison workspace" in claude_md.read_text(encoding="utf-8")

    claude_md.write_text("# Hand-edited operator notes\n", encoding="utf-8")
    run_grison("sync")

    assert claude_md.read_text(encoding="utf-8") == "# Hand-edited operator notes\n"


def test_first_run_bootstrap_creates_env_template_and_exits_with_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``run_grison``/``workspace`` fixture here on purpose — those pre-seed
    ``.grison/env`` with working fake creds; this test needs a genuinely fresh
    directory to exercise the first-run bootstrap path (which bails out before any
    Ghostwriter/BookStack client is ever constructed, so no fake transport is
    needed)."""
    root = tmp_path / "fresh-workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    for key in ("GRISON_GW_URL", "GRISON_GW_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    import grison.cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, ["sync"])

    env_path = root / ".grison" / "env"
    # ENGINE.md §10: missing credentials is "could not run" — exit 2, not 1 (item
    # 6, fix-fin1 — this test used to assert exit 1).
    assert result.exit_code == 2
    assert env_path.exists()
    assert "GRISON_GW_URL=" in env_path.read_text(encoding="utf-8")
    assert oct(env_path.stat().st_mode)[-3:] == "600"
    assert "missing Ghostwriter credentials" in result.output
    assert "Fill them into .grison/env" in result.output


# ---------------------------------------------------------------------------
# Bug fix: a failed server-compatibility check is "could not run" (ENGINE.md
# §10) — exit code 2 — never a per-phase failure (exit 1). BEFORE the fix, only
# grison.remote.compat.SchemaCompatibilityError itself mapped to exit 2 in
# grison.cli's sync(); a plain GhostwriterError raised by
# check_ghostwriter_compatibility's own probe/introspection calls (introspection
# denied, a transport failure surviving retries) missed that narrow
# `except SchemaCompatibilityError` and fell through to `_guarded`'s catch-all,
# exiting 1 instead. check_ghostwriter_compatibility now wraps any such
# GrisonError into SchemaCompatibilityError itself, carrying the cause's own
# message, so the CLI's narrow catch is correct without widening.
# ---------------------------------------------------------------------------


def test_sync_exits_2_when_introspection_is_denied(run_grison, gw_server, workspace) -> None:
    """The cold path's own ``introspect_schema()`` call (root field ``__schema``)
    getting a GraphQL authorization error must exit 2 with the cause's message —
    not 1."""
    from grison.remote.compat import save_cache

    save_cache(workspace, "sha256:stale-forces-the-cold-path")
    gw_server.inject_graphql_error("__schema", "not authorized to introspect this schema")

    result = run_grison("sync")

    assert result.exit_code == 2, result.output
    assert "not authorized to introspect this schema" in result.output


def test_sync_exits_2_on_a_transport_failure_during_the_probe(run_grison, gw_server) -> None:
    """A transport failure surviving retries (four straight HTTP 503s exhausting
    ``GhostwriterClient``'s default ``max_attempts``) during the cheap
    ``fingerprint_probe()`` request — the very first thing every sync does,
    warm-cache or not — must also exit 2, not 1."""
    gw_server.inject_http_status(503, times=4)

    result = run_grison("sync")

    assert result.exit_code == 2, result.output
    assert "503" in result.output


# ---------------------------------------------------------------------------
# Workspace-level WS-* rules gate sync/undo outright (item 1, fix-fin1)
# ---------------------------------------------------------------------------


def test_sync_refuses_when_a_canonical_deny_rule_is_hand_removed(
    run_grison, workspace, gw_server, bs_server, monkeypatch
) -> None:
    """Item 1 (CRITICAL, fix-fin1): before this fix, every sync phase filtered
    ``validate_workspace``'s failures down to its own path prefix
    (``findings``/``methodology``/``findings/reports``) — a WS-012 failure (whose
    path is ``.claude/settings.json``, matching none of those prefixes) was
    silently dropped, so a hand-edit removing one of grison's own deny rules never
    gated anything. Now: any WS-* failure refuses the WHOLE run outright, before
    the first remote request of any kind."""
    run_grison("sync")  # real bootstrap + scaffold — settings.json now carries the
    # canonical baseline, recorded in .grison/state/mirrors.json

    settings_path = workspace / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    assert "Bash(*grison sync*)" in deny
    deny.remove("Bash(*grison sync*)")
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    gw_before, bs_before = len(gw_server.request_log), len(bs_server.request_log)
    import grison.cli as cli_mod

    def _no_remote_client(*a: object, **k: object) -> None:
        raise AssertionError("no remote client should ever be constructed")

    monkeypatch.setattr(cli_mod, "_make_gw_client", _no_remote_client)
    monkeypatch.setattr(cli_mod, "_make_bs_client", _no_remote_client)

    result = run_grison("sync")

    assert result.exit_code == 2, result.output
    assert "WS-012" in result.output
    assert ".claude/settings.json" in result.output
    assert len(gw_server.request_log) == gw_before  # not one request of any kind
    assert len(bs_server.request_log) == bs_before


def test_undo_refuses_when_a_canonical_deny_rule_is_hand_removed(
    run_grison, workspace, gw_server, bs_server, monkeypatch
) -> None:
    """Item 1, ``grison undo`` half: before this fix, ``undo`` ran no validation at
    all. A snapshot exists (from a real push) but the same hand-edit must still
    refuse the undo outright, before any replay."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")  # pull
    path = workspace / "methodology" / "library" / "playbook" / "notes.md"
    path.write_text("---\ntitle: Notes\n---\n\n# Edited\n", encoding="utf-8")
    run_grison("sync")  # push -> a real snapshot exists to undo

    settings_path = workspace / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    assert "Bash(*grison undo*)" in deny
    deny.remove("Bash(*grison undo*)")
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    bs_before = len(bs_server.request_log)
    import grison.cli as cli_mod

    def _no_remote_client(*a: object, **k: object) -> None:
        raise AssertionError("no remote client should ever be constructed")

    monkeypatch.setattr(cli_mod, "_make_bs_client", _no_remote_client)

    result = run_grison("undo")

    assert result.exit_code == 2, result.output
    assert "WS-012" in result.output
    assert len(bs_server.request_log) == bs_before  # not one request — no replay at all


def test_grison_scaffold_force_lets_sync_proceed_again(
    run_grison, workspace, gw_server, bs_server
) -> None:
    """The prescribed fix for a WS-012 refusal: ``grison scaffold --force``
    recomputes ``.claude/settings.json`` from scratch (re-adding the removed deny
    rule) and re-records its digest — the very next ``sync`` must proceed
    normally again, not stay refused."""
    run_grison("sync")
    settings_path = workspace / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["permissions"]["deny"].remove("Bash(*grison sync*)")
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    refused = run_grison("sync")
    assert refused.exit_code == 2, refused.output

    scaffolded = run_grison("scaffold", "--force")
    assert scaffolded.exit_code == 0, scaffolded.output
    healed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "Bash(*grison sync*)" in healed["permissions"]["deny"]

    result = run_grison("sync")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# D13 format-mismatch gate on every command that reads a workspace (item 10)
# ---------------------------------------------------------------------------


def _write_format_1_manifest(workspace: Path) -> None:
    (workspace / ".grison" / "manifest.yml").write_text("format: 1\n", encoding="utf-8")


def test_sync_refuses_a_format_1_manifest(
    run_grison, workspace, gw_server, bs_server, monkeypatch
) -> None:
    """Item 10 (fix-fin1, D13): before this fix, only ``grison validate`` ever
    consulted the manifest format — ``sync``/``status``/``undo`` never called
    ``manifest.check()`` at all, so a workspace whose ``.grison/manifest.yml``
    says ``format: 1`` got fully synced as if it were current. Now every command
    refuses outright, before any work."""
    _write_format_1_manifest(workspace)
    before = sorted(p.relative_to(workspace) for p in workspace.rglob("*") if p.is_file())
    gw_before, bs_before = len(gw_server.request_log), len(bs_server.request_log)
    import grison.cli as cli_mod

    def _no_remote_client(*a: object, **k: object) -> None:
        raise AssertionError("no remote client should ever be constructed")

    monkeypatch.setattr(cli_mod, "_make_gw_client", _no_remote_client)
    monkeypatch.setattr(cli_mod, "_make_bs_client", _no_remote_client)

    result = run_grison("sync")

    assert result.exit_code == 2, result.output
    assert "format 1" in result.output
    assert "no migration converts it" in result.output
    after = sorted(p.relative_to(workspace) for p in workspace.rglob("*") if p.is_file())
    assert after == before  # nothing written
    assert len(gw_server.request_log) == gw_before
    assert len(bs_server.request_log) == bs_before


def test_status_refuses_a_format_1_manifest(run_grison, workspace) -> None:
    _write_format_1_manifest(workspace)
    before = sorted(p.relative_to(workspace) for p in workspace.rglob("*") if p.is_file())

    result = run_grison("status")

    assert result.exit_code == 2, result.output
    assert "format 1" in result.output
    after = sorted(p.relative_to(workspace) for p in workspace.rglob("*") if p.is_file())
    assert after == before


def test_undo_refuses_a_format_1_manifest(
    run_grison, workspace, gw_server, bs_server, monkeypatch
) -> None:
    """A real snapshot exists (from before the workspace's format was corrupted) —
    the refusal must still win over replaying it."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# Original")
    run_grison("sync")  # pull
    path = workspace / "methodology" / "library" / "playbook" / "notes.md"
    path.write_text("---\ntitle: Notes\n---\n\n# Edited\n", encoding="utf-8")
    run_grison("sync")  # push -> a real snapshot exists

    _write_format_1_manifest(workspace)
    bs_before = len(bs_server.request_log)
    import grison.cli as cli_mod

    def _no_remote_client(*a: object, **k: object) -> None:
        raise AssertionError("no remote client should ever be constructed")

    monkeypatch.setattr(cli_mod, "_make_bs_client", _no_remote_client)

    result = run_grison("undo")

    assert result.exit_code == 2, result.output
    assert "format 1" in result.output
    assert len(bs_server.request_log) == bs_before  # no replay at all


def test_sync_re_introspects_and_updates_the_cache_after_a_server_upgrade(
    run_grison,
    gw_server,
    workspace,
) -> None:
    """A stale cached fingerprint (as if Ghostwriter was upgraded since the last
    sync) forces the cold path — full introspection + validate-every-operation —
    and, since grison's own operations are still compatible, the sync succeeds
    and re-caches the NEW fingerprint so the next sync is warm again. Pins that
    wrapping probe/introspection errors into SchemaCompatibilityError (above)
    never swallows the ordinary successful cold-path flow."""
    from grison.remote.compat import fingerprint_from_schema, load_cache, save_cache
    from tests.fakes.gw_server import load_schema as load_fake_gw_schema

    save_cache(workspace, "sha256:stale-as-if-the-server-was-just-upgraded")
    before = len(gw_server.request_log)

    result = run_grison("sync")

    assert result.exit_code == 0, result.output
    assert any(op.name == "__schema" for op in gw_server.request_log[before:])  # re-introspected
    cached = load_cache(workspace)
    assert cached is not None
    assert cached.fingerprint == fingerprint_from_schema(load_fake_gw_schema())


# ---------------------------------------------------------------------------
# `sync --json` emits ONE combined document for the whole run (item 8, fix-fin1)
# ---------------------------------------------------------------------------


def test_sync_json_emits_one_combined_document(run_grison, bs_server) -> None:
    """Before this fix, ``--json`` printed one JSON-Lines blob per phase
    (findings, then report, then wiki), each repeating its own ``snapshot_dir``/
    ``exit_code`` — three separate streams a consumer had to know to
    concatenate. Now it is exactly ONE JSON document: ``{"report": …,
    "findings": …, "wiki": …, "snapshot": …, "exit_code": n}``, each phase's
    slot shaped ``{"events": [...], "summary": {...}}``."""
    book = bs_server.store.seed_book(name="Playbook")
    bs_server.store.seed_page(book_id=book["id"], name="Notes", markdown="# N")

    result = run_grison("sync", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # exactly one JSON document — json.loads
    # would raise on anything but a single well-formed document (in particular,
    # on the old JSON-Lines-per-phase shape, which is not valid JSON as a whole).
    assert set(payload) == {"report", "findings", "wiki", "snapshot", "exit_code"}
    assert payload["exit_code"] == 0
    for phase in ("report", "findings", "wiki"):
        assert set(payload[phase]) == {"events", "summary"}
        assert isinstance(payload[phase]["events"], list)
        assert isinstance(payload[phase]["summary"], dict)
    assert any(e["verb"] == "pull" for e in payload["wiki"]["events"])
    assert payload["wiki"]["summary"]["kinds"]["bs.page"]["counts"]["pull_new"] == 1


def test_sync_json_wiki_slot_is_null_when_bookstack_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiki phase never even runs without BookStack credentials — its slot in
    the combined document is `null`, distinct from `{"error": ...}` (it raised)
    or a real payload (it ran)."""
    from grison.remote.bootstrap import bootstrap_workspace

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "https://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    for key in ("GRISON_BS_URL", "GRISON_BS_TOKEN_ID", "GRISON_BS_TOKEN_SECRET"):
        monkeypatch.delenv(key, raising=False)
    bootstrap_workspace(tmp_path)

    import grison.cli as cli_mod

    monkeypatch.setattr(cli_mod, "check_ghostwriter_compatibility", lambda client, root: None)
    monkeypatch.setattr(
        cli_mod, "_run_findings_phase", lambda *a, **k: cli_mod.FindingsPhaseResult()
    )
    monkeypatch.setattr(
        cli_mod, "_run_reports_phase", lambda *a, **k: (cli_mod.ReportsPhaseResult(), {})
    )

    result = CliRunner().invoke(cli_mod.app, ["sync", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["wiki"] is None


def _strip_to_env_only(workspace: Path) -> None:
    """Leave only the hand-placed ``.grison/env`` behind, inside a git repo: the
    real-workspace shape of 'copy the credentials into a fresh clone, run sync'."""
    for name in ("manifest.yml", ".gitignore", "index.json"):
        (workspace / ".grison" / name).unlink()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)


def test_first_sync_bootstraps_a_git_repo_holding_only_a_hand_placed_env(
    run_grison, gw_server, workspace
):
    """2026-09-21 real-workspace defect: `.grison/env` copied into a fresh `git init`
    directory, then `grison sync` — refused with WS-008 ('.grison/env must be
    git-ignored') before bootstrap could write the very `.grison/.gitignore` the
    rule wants. A hand-placed env is not a bootstrapped workspace."""
    gw_server.store.seed_finding(id=1, title="Weak TLS Ciphers", severityId=3, findingTypeId=4)
    _strip_to_env_only(workspace)

    result = run_grison("sync")

    assert result.exit_code == 0, result.output
    assert "WS-008" not in result.output
    assert (workspace / ".grison" / "manifest.yml").is_file()
    assert (workspace / ".grison" / ".gitignore").is_file()
    assert (workspace / "findings" / "library" / "weak-tls-ciphers.md").is_file()


def test_dry_run_on_a_git_repo_holding_only_a_hand_placed_env_writes_nothing(run_grison, workspace):
    _strip_to_env_only(workspace)
    before = tree_snapshot(workspace)

    result = run_grison("sync", "--dry-run")

    assert result.exit_code == 2, result.output
    assert "would create" in result.output
    assert tree_snapshot(workspace) == before
