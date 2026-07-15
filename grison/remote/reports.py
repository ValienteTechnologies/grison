"""Report-narrative sync — the report's ``extraFields`` sections, 3-way per section.

Ghostwriter owns report lifecycle (grison never creates or deletes reports), so a
report's metadata is mirrored **read-only** into ``.report.yml``. The narrative —
``report.extraFields`` — is two-way: each instance-defined key is an editable markdown
file under ``narrative/``, reconciled against GW like a finding field. Direction is
derived per section from the ``.report.yml`` merge base: only-local-changed → push,
only-remote → pull, both → collision (``<key>.remote.md`` sidecar, never overwritten).

A push rewrites the report's whole ``extraFields`` map in one ``update_report`` (GW's
jsonb ``_set`` replaces the column), merging the pushed sections over the verbatim
remote map so untouched keys keep their exact remote HTML. The pre-image is snapshotted
so rollback restores every section at once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from grison.remote.repmap import (
    NARRATIVE_DIR,
    REPORT_META,
    ReportDoc,
    md_section_to_html,
    meta_to_yaml,
    read_local_meta,
    read_local_section,
    report_from_record,
    section_hash,
    section_path,
)
from grison.remote.snapshot import Snapshot
from grison.sinks.file_sink import slugify

if TYPE_CHECKING:
    from grison.remote.ghostwriter import GhostwriterClient


@dataclass
class ReportResult:
    pulled: list[Path] = field(default_factory=list)
    pushed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    repaired: list[Path] = field(default_factory=list)
    collisions: list[Path] = field(default_factory=list)
    materialized: list[Path] = field(default_factory=list)  # .report.yml metadata mirrors
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    snapshot_dir: Path | None = None
    mass_change_blocked: bool = False
    errors: list[str] = field(default_factory=list)


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _emit(on_event: Callable[[str], None] | None, msg: str) -> None:
    if on_event:
        on_event(msg)


def _report_dir(root: Path, report_id: int, title: str) -> Path:
    """Locate the report's directory — an existing ``findings/reports/<id>-*`` (so
    narrative lands beside the report's findings even after a remote title rename),
    else the canonical ``<id>-<slug>`` path."""
    base = root / "findings" / "reports"
    if base.exists():
        for d in sorted(base.glob(f"{report_id}-*")):
            if d.is_dir():
                return d
    return base / f"{report_id}-{slugify(title)}"


@dataclass
class _SectionPlan:
    action: str  # pull | push | collision | repair | clean | skip
    key: str
    path: Path
    body: str = ""  # markdown to write (pull/repair) or push source
    note: str = ""


def sync_reports(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool = False,
    force_local: set[Path] | None = None,
    force_remote: set[Path] | None = None,
    mass_change_ratio: float = 0.5,
    on_event: Callable[[str], None] | None = None,
) -> ReportResult:
    """Reconcile each report's narrative sections with Ghostwriter (3-way per section)."""
    force_local = force_local or set()
    force_remote = force_remote or set()
    result = ReportResult()
    now = datetime.now(UTC)
    snap = Snapshot()

    _emit(on_event, "pulling ghostwriter reports…")
    reports = client.fetch_reports()
    _emit(on_event, f"reports: {len(reports)}")

    planned_pushes = 0
    per_report: list[tuple[ReportDoc, Path, list[_SectionPlan]]] = []
    for rec in reports:
        doc = report_from_record(rec)
        rdir = _report_dir(root, doc.report_id, doc.title)
        _, bases = read_local_meta(rdir)
        plans = _plan_report(rdir, doc, bases, force_local, force_remote, result, on_event, root)
        planned_pushes += sum(1 for p in plans if p.action == "push")
        per_report.append((doc, rdir, plans))

    total_sections = max(sum(len(d.sections) for d, _, _ in per_report), 1)
    if not dry_run and planned_pushes > 5 and planned_pushes > mass_change_ratio * total_sections:
        result.mass_change_blocked = True
        _emit(on_event, f"mass-change guard tripped: withholding {planned_pushes} section pushes")
        for _d, _rdir, plans in per_report:
            for p in plans:
                if p.action == "push":
                    p.action = "skip"
                    p.note = "mass-change guard — push withheld"

    try:
        for doc, rdir, plans in per_report:
            try:
                _apply_report(doc, rdir, plans, client, snap, result, now,
                              dry_run=dry_run, on_event=on_event, root=root)
            except Exception as e:  # noqa: BLE001 — isolate one report, keep the batch + snapshot
                result.errors.append(f"{rdir}: {e}")
                _emit(on_event, f"error {_rel(root, rdir)}: {e}")
    finally:
        if not dry_run and not snap.empty:
            when = now.strftime("%Y%m%dT%H%M%SZ") + "-reports"
            result.snapshot_dir = snap.persist(when)
            _emit(on_event, f"snapshot → {result.snapshot_dir}")
    return result


def _plan_report(
    rdir: Path,
    doc: ReportDoc,
    bases: dict[str, str],
    force_local: set[Path],
    force_remote: set[Path],
    result: ReportResult,
    on_event: Callable[[str], None] | None,
    root: Path,
) -> list[_SectionPlan]:
    plans: list[_SectionPlan] = []
    for key, section in doc.sections.items():
        path = section_path(rdir, key)
        remote_md = section.body
        rhash = section_hash(remote_md)
        local_md = read_local_section(rdir, key)
        base = bases.get(key)

        if path in force_remote:
            plans.append(_SectionPlan("pull", key, path, remote_md))
            continue
        if path in force_local and local_md is not None:
            plans.append(_SectionPlan("push", key, path, local_md))
            continue
        if local_md is None:
            plans.append(_SectionPlan("pull", key, path, remote_md))  # never pulled → new
            continue
        lhash = section_hash(local_md)
        if base is None:
            # file exists with no merge base (hand-created for an existing key): converge
            # if it already matches remote, otherwise surface — never silently overwrite.
            plans.append(_SectionPlan("repair" if lhash == rhash else "collision", key, path,
                                      remote_md))
            continue
        if lhash == base and rhash == base:
            plans.append(_SectionPlan("clean", key, path))
        elif lhash != base and rhash == base:
            plans.append(_SectionPlan("push", key, path, local_md))
        elif lhash == base and rhash != base:
            plans.append(_SectionPlan("pull", key, path, remote_md))
        elif lhash == rhash:
            plans.append(_SectionPlan("repair", key, path, remote_md))
        else:
            plans.append(_SectionPlan("collision", key, path, remote_md))

    # local narrative files whose key no longer exists remotely
    ndir = rdir / NARRATIVE_DIR
    if ndir.exists():
        for md in sorted(ndir.glob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            key = md.stem
            if key in doc.sections:
                continue
            note = ("remote section gone — kept locally" if key in bases
                    else f"unknown report field '{key}' — define it in Ghostwriter first")
            result.skipped.append((md, note))
            _emit(on_event, f"skip {_rel(root, md)}: {note}")
    return plans


def _write_section(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.strip() + "\n" if body.strip() else "", encoding="utf-8")


def _apply_report(
    doc: ReportDoc,
    rdir: Path,
    plans: list[_SectionPlan],
    client: GhostwriterClient,
    snap: Snapshot,
    result: ReportResult,
    now: datetime,
    *,
    dry_run: bool,
    on_event: Callable[[str], None] | None,
    root: Path,
) -> None:
    pushes = [p for p in plans if p.action == "push"]
    for p in plans:
        if p.action == "clean":
            result.unchanged.append(p.path)
        elif p.action == "skip":
            result.skipped.append((p.path, p.note))
            _emit(on_event, f"skip {_rel(root, p.path)}: {p.note}")
        elif p.action == "pull":
            if dry_run:
                result.pulled.append(p.path)
                _emit(on_event, f"would pull {_rel(root, p.path)}")
            else:
                _write_section(p.path, p.body)
                result.pulled.append(p.path)
                _emit(on_event, f"pull {_rel(root, p.path)}")
        elif p.action == "repair":
            if not dry_run:
                _write_section(p.path, p.body)
            result.repaired.append(p.path)
            _emit(on_event, f"{'would ' if dry_run else ''}repair {_rel(root, p.path)}")
        elif p.action == "collision":
            if not dry_run:
                sidecar = p.path.with_name(f"{p.key}.remote.md")
                _write_section(sidecar, p.body)
            result.collisions.append(p.path)
            _emit(on_event, f"{'would ' if dry_run else ''}collision {_rel(root, p.path)}")

    if pushes and not dry_run:
        # one PUT for the whole report: merge pushed sections over the verbatim remote map
        new_extra = dict(doc.raw_extra_fields)
        for p in pushes:
            new_extra[p.key] = md_section_to_html(p.body)
        snap.before_update_report(doc.report_id, doc.raw_extra_fields)
        client.update_report(doc.report_id, {"extraFields": new_extra})
        for p in pushes:
            result.pushed.append(p.path)
            _emit(on_event, f"push {_rel(root, p.path)}")
    elif pushes:  # dry-run
        for p in pushes:
            result.pushed.append(p.path)
            _emit(on_event, f"would push {_rel(root, p.path)}")

    # refresh the read-only metadata mirror + section merge bases (skip in dry-run)
    if not dry_run:
        final_hashes = _final_section_hashes(rdir, doc, plans)
        meta_path = rdir / REPORT_META
        text = meta_to_yaml(doc, final_hashes)
        if not meta_path.exists() or meta_path.read_text(encoding="utf-8") != text:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(text, encoding="utf-8")
            result.materialized.append(meta_path)
            _emit(on_event, f"report meta {_rel(root, meta_path)}")


def _final_section_hashes(
    rdir: Path, doc: ReportDoc, plans: list[_SectionPlan]
) -> dict[str, str]:
    """The merge base to stamp after applying: a pushed/clean/repaired section converges
    on its content; a collision keeps the prior base (unresolved). Only real remote keys
    get a base."""
    action_by_key = {p.key: p.action for p in plans}
    hashes: dict[str, str] = {}
    _, prior = read_local_meta(rdir)
    for key, section in doc.sections.items():
        action = action_by_key.get(key, "clean")
        if action == "collision":
            if key in prior:
                hashes[key] = prior[key]  # leave the base untouched until resolved
            continue
        # pull/repair converge on remote; push converges on local; clean already matches
        local_md = read_local_section(rdir, key)
        hashes[key] = section_hash(local_md) if local_md is not None else section_hash(section.body)
    return hashes
