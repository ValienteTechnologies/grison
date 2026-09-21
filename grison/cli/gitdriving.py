"""Git-driving helpers (auto-commit around parse/sync) — see :mod:`grison.cli` for
the CLI itself."""

from __future__ import annotations

from pathlib import Path

import typer

from grison import gitdrive
from grison.adapters.bs_pages import BsPageAdapter
from grison.adapters.gw_report import NarrativeSectionAdapter
from grison.cli.phases.findings import FindingsPhaseResult
from grison.cli.phases.reports import ReportsPhaseResult
from grison.cli.phases.wiki import WikiPhaseResult
from grison.remote.creds import Settings
from grison.sinks import ParseSummary


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
