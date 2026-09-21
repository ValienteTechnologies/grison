"""Phase-6 CLI tests: parse fills findings/inbox/, status validates it."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grison.cli import FindingsPhaseResult, _format_reasons, app
from grison.engine.model import Event, KindSummary, Outcome, Plan

_FIX = Path(__file__).parent / "fixtures" / "scanners"
_runner = CliRunner()
_GW_CREDS_VARS = (
    "GRISON_GW_URL",
    "GRISON_GW_TOKEN",
    "GRISON_CF_CLIENT_ID",
    "GRISON_CF_CLIENT_SECRET",
)

# A minimal, format-v2-valid library finding — used by the git-driving tests below to
# simulate a sync writing a new file: gitdrive.commit() now validates before
# committing (brief D11 item 6), so the synthetic content a fake sync phase writes
# must itself be a document that validates clean, not just an arbitrary placeholder.
_VALID_LIBRARY_FINDING = (
    "---\nseverity: low\nfinding_type: web\n---\n"
    "# x\n\n"
    "## Description\n\nx\n\n"
    "## Impact\n\nx\n\n"
    "## Mitigation\n\nx\n\n"
    "## Replication Steps\n\nx\n\n"
    "## References\n\nx\n"
)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "grison-test"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def _log_subjects(path: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=path, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()  # newest first


def _rev_count(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _set_fake_ghostwriter_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRISON_GW_URL", "https://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")


def _stub_sync_phases(monkeypatch: pytest.MonkeyPatch, run_sync=None, sync_reports=None) -> None:
    import grison.cli.commands.sync as sync_mod
    import grison.cli.phases.findings as findings_phase_mod
    import grison.cli.phases.reports as reports_phase_mod
    from grison.cli import ReportsPhaseResult

    monkeypatch.setattr(sync_mod, "check_ghostwriter_compatibility", lambda client, root: None)
    monkeypatch.setattr(
        findings_phase_mod,
        "_run_findings_phase",
        run_sync or (lambda *a, **k: FindingsPhaseResult()),
    )
    monkeypatch.setattr(
        reports_phase_mod,
        "_run_reports_phase",
        sync_reports or (lambda *a, **k: (ReportsPhaseResult(), {})),
    )


def test_help_lists_verbs() -> None:
    result = _runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "parse" in result.output and "status" in result.output


def test_parse_bootstraps_and_status_reports_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tests changed on purpose (task C, BRIEF "Standing calls"): the old paths-taking
    # `grison status <path>` (v1 per-file finding validation) is gone — `grison
    # validate` is the offline per-document validity command now, and the new
    # `grison status` (no paths) is a whole-workspace overview instead. Findings are
    # engine-managed (format v2) same as everything else, so `grison status` reports
    # a real per-record breakdown for them too, not just a plain file count.
    #
    # tests changed on purpose (bug fix): `grison parse` in an empty directory now
    # scaffolds the COMPLETE workspace via `bootstrap_workspace` (spec §10 /
    # bootstrap.py's own docstring: "a first `grison sync`/`grison parse` in an
    # empty directory yields a complete, valid, self-contained workspace") — it is
    # still fully offline/credential-free (bootstrap_workspace never contacts a
    # remote, it only ever writes a template env), so `.grison/manifest.yml` and
    # `index.json` exist right after `parse` with no manual `.grison` mkdir needed
    # before `status`/`validate` can run.
    scans = tmp_path / "scans"
    scans.mkdir()
    for name in ("burp_sample.xml", "nessus_sample.xml", "sslyze_sample.json"):
        shutil.copy(_FIX / name, scans / name)
    monkeypatch.chdir(tmp_path)  # workspace root = tmp_path

    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 0, r.output
    assert "Parsed" in r.output
    inbox = tmp_path / "findings" / "inbox"
    assert (tmp_path / "findings" / "library").is_dir()  # full tree scaffolded
    md_files = list(inbox.glob("*.md"))
    assert md_files
    assert (tmp_path / ".grison" / "manifest.yml").is_file()
    assert (tmp_path / ".grison" / "index.json").is_file()

    r2 = _runner.invoke(app, ["status"])
    assert r2.exit_code == 0, r2.output
    assert "findings" in r2.output  # findings are now engine-managed too (task step 3)
    assert "methodology:" in r2.output


def test_parse_in_empty_dir_then_validate_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug fix: BEFORE the fix, `grison parse` in a brand-new empty directory used
    the bare `bootstrap_tree` helper, so no `.grison/manifest.yml`/`index.json` ever
    got written and the very next `grison validate` exited 2 "no grison workspace
    found" — even though `parse` itself reported success. `grison validate` must
    exit 0 straight after, and the parsed inbox document must carry no `grison:`
    frontmatter block (D3: documents carry no machine fields, inbox included)."""
    monkeypatch.chdir(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0, r.output

    inbox_files = list((tmp_path / "findings" / "inbox").glob("*.md"))
    assert inbox_files
    for f in inbox_files:
        assert "grison:" not in f.read_text(encoding="utf-8")

    r2 = _runner.invoke(app, ["validate"])
    assert r2.exit_code == 0, r2.output


def test_parse_unrecognized_file_exits_1_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug fix: a file no scanner recognizes used to only land in skipped_files
    # (never `errors`), so `grison parse` exited 0 having done nothing useful with
    # it. Policy: exit 1, with the file named.
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "notes.txt").write_text("not a scan\n")
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["parse", str(scans)])
    assert r.exit_code == 1
    assert "skipped" in r.output and "notes.txt" in r.output


def test_parse_nonexistent_path_exits_2_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug fix: a path argument that doesn't exist is "could not run", not "nothing
    # to do" — exit 2, and nothing gets scaffolded/parsed/written.
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "does-not-exist"
    r = _runner.invoke(app, ["parse", str(missing)])
    assert r.exit_code == 2
    assert "does-not-exist" in r.output
    assert not (tmp_path / "findings").exists()
    assert not (tmp_path / ".grison").exists()


def test_parse_zero_findings_from_recognized_file_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A recognized file that simply produces no findings (e.g. everything filtered
    # out by --min-severity) is a clean, successful run — exit 0, not an error.
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(
        app,
        ["parse", str(_FIX / "burp_sample.xml"), "--min-severity", "critical"],
    )
    assert r.exit_code == 0, r.output
    assert list((tmp_path / "findings" / "inbox").glob("*.md")) == []


def test_parse_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml"), "--dry-run"])
    assert r.exit_code == 0
    assert "Would write" in r.output
    assert list((tmp_path / "findings" / "inbox").glob("*.md")) == []


def test_parse_summary_uses_words_not_an_arrow_glyph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 9 (LOW, fix-fin1): ``_print_parse_summary`` used to print a literal
    ``→`` glyph ("Wrote N → <dir>") — every other printer in grison spells things
    out in words (ENGINE.md 'Events': "No arrow glyphs"). ``parse``'s own summary
    line is not an engine event, but the same house style applies."""
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0
    assert "→" not in r.output
    assert "to " in r.output


def test_sync_exit_code_reflects_result_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that finishes with isolated per-record errors must still exit non-zero —
    a green summary line next to a swallowed error would be misleading."""
    import grison.cli.commands.sync as sync_mod
    import grison.cli.phases.findings as findings_phase_mod
    import grison.cli.phases.reports as reports_phase_mod
    from grison.cli import ReportsPhaseResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "https://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")

    def fake_run_sync(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        evidence_by_report=None,
        snapshot=None,
    ):
        plan = Plan(
            kind="gw.finding",
            outcome=Outcome.FAILED,
            path=Path("findings/library/bad.md"),
            reason="boom",
        )
        summary = KindSummary(kind="gw.finding")
        summary.bump(Outcome.FAILED)
        event = Event(verb="failed", path="findings/library/bad.md", detail="boom")
        return FindingsPhaseResult(plans=[plan], events=[event], summaries={"gw.finding": summary})

    def fake_reports_phase(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        snapshot=None,
        quiet=False,
    ):
        return ReportsPhaseResult(), {}

    monkeypatch.setattr(sync_mod, "check_ghostwriter_compatibility", lambda client, root: None)
    monkeypatch.setattr(findings_phase_mod, "_run_findings_phase", fake_run_sync)
    monkeypatch.setattr(reports_phase_mod, "_run_reports_phase", fake_reports_phase)
    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1
    assert "boom" in r.output


def test_sync_info_severity_skip_does_not_flip_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test changed on purpose (engine step 3): the v1 pipeline had a separate
    non-fatal "warnings" list (a canonicalized construct, a recomputed cvss score)
    that never affected the exit code. The engine has no such list — every
    validation problem is a hard failure (INVALID) now — but it keeps the same
    "something the user need not act on must not fail the run" property for an
    INFO-severity SKIP (a draft/template record, ENGINE.md's exit-code policy):
    it's printed (with --verbose) but never flips the exit code."""
    import grison.cli.commands.sync as sync_mod
    import grison.cli.phases.findings as findings_phase_mod
    import grison.cli.phases.reports as reports_phase_mod
    from grison.cli import ReportsPhaseResult
    from grison.engine.model import VetoSeverity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GW_URL", "https://gw.test")
    monkeypatch.setenv("GRISON_GW_TOKEN", "tok")
    monkeypatch.setenv("GRISON_CF_CLIENT_ID", "cid")
    monkeypatch.setenv("GRISON_CF_CLIENT_SECRET", "csecret")

    def fake_run_sync(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        evidence_by_report=None,
        snapshot=None,
    ):
        plan = Plan(
            kind="gw.finding",
            outcome=Outcome.SKIP,
            path=Path("findings/library/f.md"),
            reason="a draft, skipped",
            severity=VetoSeverity.INFO,
        )
        summary = KindSummary(kind="gw.finding")
        summary.bump(Outcome.SKIP)
        event = Event(
            verb="skip",
            path="findings/library/f.md",
            detail="a draft, skipped",
            severity=VetoSeverity.INFO,
        )
        return FindingsPhaseResult(plans=[plan], events=[event], summaries={"gw.finding": summary})

    def fake_reports_phase(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        snapshot=None,
        quiet=False,
    ):
        return ReportsPhaseResult(), {}

    monkeypatch.setattr(sync_mod, "check_ghostwriter_compatibility", lambda client, root: None)
    monkeypatch.setattr(findings_phase_mod, "_run_findings_phase", fake_run_sync)
    monkeypatch.setattr(reports_phase_mod, "_run_reports_phase", fake_reports_phase)
    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output


def test_validate_flags_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tests changed on purpose: ``grison validate`` (offline, format v2) replaced
    the old paths-taking ``grison status <path>`` (v1 per-file validity) — see
    test_parse_bootstraps_and_status_reports_valid's own note."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".grison").mkdir()
    (tmp_path / "findings" / "library").mkdir(parents=True)
    bad = tmp_path / "findings" / "library" / "bad.md"
    # a format-v2 finding with an unrecognized frontmatter field (FND-001)
    bad.write_text(
        "---\nseverity: low\nfinding_type: host\nbogus_field: nope\n---\n\n"
        "# Bad\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    r = _runner.invoke(app, ["validate", str(bad)])
    assert r.exit_code == 1
    assert "FND-001" in r.output


# --- GRISON_GIT git-driving (Feature B) --------------------------------------------


def test_sync_git_driving_commits_checkpoint_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    (tmp_path / "pre.txt").write_text("dirty before sync even starts\n")

    def fake_run_sync(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        evidence_by_report=None,
        snapshot=None,
    ):
        (root / "findings" / "library" / "new.md").write_text(_VALID_LIBRARY_FINDING)
        summary = KindSummary(kind="gw.finding")
        summary.counts = {"pull": 1, "push": 1}
        return FindingsPhaseResult(summaries={"gw.finding": summary})

    _stub_sync_phases(monkeypatch, run_sync=fake_run_sync)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output

    subjects = _log_subjects(tmp_path)
    assert subjects[0].startswith("grison: sync (")
    assert "findings: pull 1 push 1" in subjects[0]
    assert subjects[1] == "grison: pre-sync checkpoint"
    assert subjects[2] == "seed"
    assert not subprocess.run(  # tree clean after both checkpoints landed
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_sync_git_driving_notes_failures_in_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    def fake_run_sync(
        root,
        client,
        *,
        dry_run=False,
        force_local=None,
        force_remote=None,
        allow_mass_change=False,
        evidence_by_report=None,
        snapshot=None,
    ):
        (root / "findings" / "library" / "new.md").write_text(_VALID_LIBRARY_FINDING)
        plan = Plan(
            kind="gw.finding",
            outcome=Outcome.FAILED,
            path=Path("findings/library/bad.md"),
            reason="boom",
        )
        summary = KindSummary(kind="gw.finding")
        summary.bump(Outcome.FAILED)
        event = Event(verb="failed", path="findings/library/bad.md", detail="boom")
        return FindingsPhaseResult(plans=[plan], events=[event], summaries={"gw.finding": summary})

    _stub_sync_phases(monkeypatch, run_sync=fake_run_sync)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 1  # the phase error still fails the command

    subjects = _log_subjects(tmp_path)
    assert subjects[0].startswith("grison: sync (with failures")  # but state was still captured


def test_sync_git_driving_scoped_to_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace may be a subdir of a larger repo — grison must never stage or
    commit files outside it."""
    _init_repo(tmp_path)
    (tmp_path / "outside.txt").write_text("outside\n")
    workspace = tmp_path / "engagement"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert "outside.txt" in status  # untouched, still dirty
    show = subprocess.run(
        ["git", "show", "--stat", "--pretty=", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "outside.txt" not in show


def test_sync_git_driving_silent_when_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # never a git repo
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "git" not in r.output.lower()  # detection is the feature: silent, not a warning
    assert not (tmp_path / ".git").exists()  # never inits one either


def test_sync_git_driving_disabled_makes_no_git_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tests changed on purpose (task "self-contained workspace", grison/scaffold/
    # precommit.py): a git repo now gets its pre-commit hook installed regardless of
    # GRISON_GIT (D11's hook is an independent, always-on enforcement point, not tied
    # to whether grison itself drives commits) — that scaffolding step calls
    # gitdrive.is_repo() on its own. What GRISON_GIT actually gates is gitdrive.commit()
    # ever being called; that part of the contract is unchanged and still checked here.
    import grison.gitdrive as gitdrive_mod

    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.delenv("GRISON_GIT", raising=False)
    _init_repo(tmp_path)  # it IS a repo — but the setting is off, so it must be ignored
    calls: list[str] = []
    monkeypatch.setattr(gitdrive_mod, "commit", lambda root, msg: calls.append("commit") or True)
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert calls == []  # gitdrive.commit() itself is never called when the setting is off


def test_sync_dry_run_never_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # item 2, fix-fin1: `--dry-run` on a never-bootstrapped directory now refuses
    # outright (exit 2, "would create" report — see test_sync_dry_run_on_a_fresh_
    # directory_refuses_and_writes_nothing below) rather than bootstrapping it for
    # real; this test is about the git-commit behavior specifically, so it
    # bootstraps first (bypassing the CLI, like the real first sync would) and
    # only then exercises the dry-run.
    from grison.remote.bootstrap import bootstrap_workspace

    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    bootstrap_workspace(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted before the dry-run\n")
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"  # only the seed commit


def test_sync_dry_run_on_a_fresh_directory_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2 (CRITICAL, fix-fin1): before this fix, ``sync --dry-run`` in a never-
    bootstrapped directory still called ``bootstrap_workspace`` for real (~11
    files written) before ever consulting ``dry_run`` — a "preview" that wrote a
    complete workspace to disk. Now it reports what a real run would create and
    stops, exit 2, writing nothing at all."""
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)

    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    r = _runner.invoke(app, ["sync", "--dry-run"])
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    assert r.exit_code == 2, r.output
    assert "not a grison workspace yet" in r.output
    assert "would create" in r.output
    assert before == after == []  # byte-identical: nothing at all was written


def test_sync_git_failure_warns_not_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_fake_ghostwriter_creds(monkeypatch)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x\n")
    (tmp_path / ".git" / "index.lock").write_text("")  # simulate a stuck git process
    _stub_sync_phases(monkeypatch)

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output  # grison's outcome never depends on git state
    assert "git:" in r.output


def test_parse_commits_when_git_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0, r.output
    assert _log_subjects(tmp_path)[0].startswith("grison: parse burp")


def test_parse_no_commit_when_git_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRISON_GIT", raising=False)
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml")])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"  # only the seed commit


def test_parse_dry_run_never_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRISON_GIT", "commit")
    _init_repo(tmp_path)

    r = _runner.invoke(app, ["parse", str(_FIX / "burp_sample.xml"), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert _rev_count(tmp_path) == "1"


# --- CLAUDE.md scaffold (Feature C) -------------------------------------------------


def test_sync_prints_claude_md_scaffold_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in _GW_CREDS_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRISON_CLAUDE_MD", raising=False)

    r = _runner.invoke(app, ["sync"])
    # item 6, fix-fin1: missing creds is "could not run" — exit 2, not 1.
    assert r.exit_code == 2  # still fails on missing creds
    assert "Scaffolded workspace + wrote CLAUDE.md" in r.output
    assert (tmp_path / "CLAUDE.md").exists()


def test_sync_claude_md_off_suppresses_scaffold_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in _GW_CREDS_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRISON_CLAUDE_MD", "off")

    r = _runner.invoke(app, ["sync"])
    assert r.exit_code == 2  # item 6, fix-fin1: missing creds — exit 2, not 1.
    assert "CLAUDE.md" not in r.output
    assert not (tmp_path / "CLAUDE.md").exists()


# --- `grison scaffold` + `grison hook post-edit` ------------------------------------


def test_scaffold_command_creates_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    r = _runner.invoke(app, ["scaffold"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".grison" / "SPEC.md").exists()
    assert "CLAUDE.md: created" in r.output


def test_scaffold_command_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _runner.invoke(app, ["scaffold"])
    r = _runner.invoke(app, ["scaffold"])
    assert r.exit_code == 0, r.output
    assert "CLAUDE.md: up-to-date" in r.output


def test_scaffold_force_reports_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _runner.invoke(app, ["scaffold"])
    r = _runner.invoke(app, ["scaffold", "--force"])
    assert r.exit_code == 0, r.output
    assert "updated .claude/settings.json" in r.output or "CLAUDE.md: regenerated" in r.output


def test_hook_post_edit_reports_clean_file_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _runner.invoke(app, ["scaffold"])
    good = tmp_path / "findings" / "library" / "good.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    payload = json.dumps({"tool_input": {"file_path": str(good)}, "cwd": str(tmp_path)})
    r = _runner.invoke(app, ["hook", "post-edit"], input=payload)
    assert r.exit_code == 0
    assert r.output.strip() == ""


def test_hook_post_edit_reports_invalid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _runner.invoke(app, ["scaffold"])
    bad = tmp_path / "findings" / "library" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\nseverity: nope\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    payload = json.dumps({"tool_input": {"file_path": str(bad)}, "cwd": str(tmp_path)})
    r = _runner.invoke(app, ["hook", "post-edit"], input=payload)
    assert r.exit_code == 0  # PostToolUse can never fail the command
    assert "FND-003" in r.output


def test_status_reasons_are_deduplicated_with_counts() -> None:
    assert _format_reasons(("WIKI-010",) * 38 + ("WIKI-012",)) == "WIKI-010 x38, WIKI-012"
    assert _format_reasons(("FND-015",)) == "FND-015"
