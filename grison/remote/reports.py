"""Report-narrative sync — the report's ``extraFields`` sections, 3-way per section.

Ghostwriter owns report lifecycle (grison never creates or deletes reports), so a
report's metadata is mirrored **read-only** into ``.report.yml``. The narrative —
``report.extraFields`` — is two-way: each instance-defined key is an editable markdown
file under ``narrative/``, reconciled against GW like a finding field. Direction is
derived per section from the ``.report.yml`` merge base: only-local-changed → push,
only-remote → pull, both → collision (``<key>.remote.md`` sidecar, never overwritten).

A push rewrites the report's whole ``extraFields`` map in one ``update_report`` (GW's
jsonb ``_set`` replaces the column). Immediately before that write, the report is
re-fetched fresh and the pushed sections are merged over *that* map — not the
top-of-run snapshot — so a concurrent remote edit to an untouched section survives
instead of being silently clobbered by a stale merge; if an untouched section drifted
between planning and this write, the whole report's push is aborted as a collision
(``<key>.remote.md`` sidecar) rather than guessed at. The undo pre-image is that same
fresh fetch, so rollback restores exactly what was live right before the write.
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
    html_section_to_md,
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
    # non-fatal: canonicalized/dropped constructs (converter on_loss) — never flip the
    # exit code by themselves.
    warnings: list[str] = field(default_factory=list)


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _emit(on_event: Callable[[str], None] | None, msg: str) -> None:
    if on_event:
        on_event(msg)


def _surface_losses(result: ReportResult, root: Path, path: Path, losses: list[str]) -> None:
    """Drain buffered on_loss messages (repmap html->md, tagged with section key by the
    caller) into ``result.warnings`` — called only where the remote body is actually
    written to disk (pull or a collision sidecar), mirroring sync.py's
    ``_surface_remote_losses``."""
    for msg in losses:
        result.warnings.append(f"{_rel(root, path)}: {msg}")


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
    # on_loss messages from converting this section's remote HTML (buffered at
    # report_from_record time) — surfaced to result.warnings only when this plan
    # actually writes the remote body to disk (pull/collision), mirroring sync.py's
    # _Plan.remote_losses so a permanently-unconvertible construct doesn't re-warn
    # every sync just from being compared against the local hash.
    losses: list[str] = field(default_factory=list)


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
        try:
            # buffered per section key, not surfaced yet — report_from_record converts
            # every section's remote HTML up front (needed for the hash comparison
            # below regardless of outcome), so on_loss firing here must not become a
            # result.warnings entry unless the section's plan actually writes the
            # remote body to disk (pull/collision) — see _SectionPlan.losses.
            losses: dict[str, list[str]] = {}
            doc = report_from_record(
                rec,
                on_loss=lambda key, msg, losses=losses: losses.setdefault(key, []).append(msg),
            )
            rdir = _report_dir(root, doc.report_id, doc.title)
            _, bases, _removed = read_local_meta(rdir)
            plans = _plan_report(
                rdir, doc, bases, force_local, force_remote, result, on_event, root, losses
            )
        except Exception as e:  # noqa: BLE001 — isolate one report's planning, keep the batch
            rid = rec.get("id")
            result.errors.append(f"report {rid}: {e}")
            _emit(on_event, f"error report {rid}: {e}")
            continue
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
    losses: dict[str, list[str]] | None = None,
) -> list[_SectionPlan]:
    losses = losses or {}
    plans: list[_SectionPlan] = []
    for key, section in doc.sections.items():
        path = section_path(rdir, key)
        remote_md = section.body
        rhash = section_hash(remote_md)
        local_md = read_local_section(rdir, key)
        base = bases.get(key)
        section_losses = losses.get(key, [])

        if path in force_remote:
            plans.append(_SectionPlan("pull", key, path, remote_md, losses=section_losses))
            continue
        if path in force_local and local_md is not None:
            plans.append(_SectionPlan("push", key, path, local_md))
            continue
        if local_md is None:
            # never pulled → new
            plans.append(_SectionPlan("pull", key, path, remote_md, losses=section_losses))
            continue
        lhash = section_hash(local_md)
        if base is None:
            # file exists with no merge base (hand-created for an existing key): converge
            # if it already matches remote, otherwise surface — never silently overwrite.
            action = "repair" if lhash == rhash else "collision"
            plans.append(_SectionPlan(action, key, path, remote_md,
                                      losses=section_losses if action == "collision" else []))
            continue
        if lhash == base and rhash == base:
            plans.append(_SectionPlan("clean", key, path))
        elif lhash != base and rhash == base:
            plans.append(_SectionPlan("push", key, path, local_md))
        elif lhash == base and rhash != base:
            plans.append(_SectionPlan("pull", key, path, remote_md, losses=section_losses))
        elif lhash == rhash:
            plans.append(_SectionPlan("repair", key, path, remote_md))
        else:
            plans.append(_SectionPlan("collision", key, path, remote_md, losses=section_losses))

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


def _fetch_report_extra_fields(client: GhostwriterClient, report_id: int) -> dict | None:
    """Re-fetch a single report's live ``extraFields`` right before a push (the client
    has no by-id report query, so this re-lists and filters). ``None`` if the report
    no longer exists remotely."""
    for rec in client.fetch_reports():
        if rec.get("id") == report_id:
            return dict(rec.get("extraFields") or {})
    return None


def _drifted_untouched_keys(doc: ReportDoc, fresh: dict | None, pushed_keys: set[str]) -> set[str]:
    """Keys in ``doc.sections`` this run isn't pushing whose live remote value (the
    fresh re-fetch taken right before the write) no longer matches the top-of-run
    snapshot — a concurrent edit landed after this run planned and before it wrote."""
    if fresh is None:
        return set()
    return {
        key for key in doc.sections
        if key not in pushed_keys and doc.raw_extra_fields.get(key) != fresh.get(key)
    }


def _guard_stale_push(
    doc: ReportDoc,
    rdir: Path,
    plans: list[_SectionPlan],
    pushes: list[_SectionPlan],
    client: GhostwriterClient,
    result: ReportResult,
    on_event: Callable[[str], None] | None,
    root: Path,
) -> dict | None:
    """Re-fetch this report's extraFields immediately before the write. If a section
    NOT being pushed this run drifted since the top-of-run snapshot (or the report
    vanished), abort the whole report's push as a collision — surfacing the drift via
    the usual ``.remote.md`` sidecar — instead of clobbering it with the stale merge.
    Returns the fresh extraFields to push over when safe, ``None`` when aborted."""
    fresh = _fetch_report_extra_fields(client, doc.report_id)
    pushed_keys = {p.key for p in pushes}
    drifted = _drifted_untouched_keys(doc, fresh, pushed_keys)
    if fresh is not None and not drifted:
        return fresh

    plan_by_key = {p.key: p for p in plans}
    for key in drifted:
        losses: list[str] = []
        body = html_section_to_md((fresh or {}).get(key) or "", on_loss=losses.append)
        plan = plan_by_key.get(key)
        if plan is not None:
            plan.action, plan.body, plan.note, plan.losses = (
                "collision", body, "concurrent remote edit", losses,
            )
        else:
            plans.append(_SectionPlan("collision", key, section_path(rdir, key), body,
                                       "concurrent remote edit", losses=losses))

    if fresh is None:
        reason = "report no longer exists remotely"
        result.errors.append(f"{rdir}: push withheld — {reason}")
    else:
        reason = f"concurrent remote edit to {', '.join(sorted(drifted))}"
    for p in pushes:
        p.action, p.note = "skip", f"push withheld — {reason}"
    return None


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
    fresh_extra: dict | None = None
    if pushes and not dry_run:
        fresh_extra = _guard_stale_push(doc, rdir, plans, pushes, client, result, on_event, root)
        if fresh_extra is None:
            pushes = []  # aborted — see _guard_stale_push's collision/skip plan rewrites

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
                _surface_losses(result, root, p.path, p.losses)
        elif p.action == "repair":
            if not dry_run:
                _write_section(p.path, p.body)
            result.repaired.append(p.path)
            _emit(on_event, f"{'would ' if dry_run else ''}repair {_rel(root, p.path)}")
        elif p.action == "collision":
            if not dry_run:
                sidecar = p.path.with_name(f"{p.key}.remote.md")
                _write_section(sidecar, p.body)
                _surface_losses(result, root, sidecar, p.losses)
            result.collisions.append(p.path)
            _emit(on_event, f"{'would ' if dry_run else ''}collision {_rel(root, p.path)}")

    if pushes and not dry_run:
        # one PUT for the whole report: merge pushed sections over the FRESH remote map
        # (re-fetched in _guard_stale_push right before this write — never the stale
        # top-of-run snapshot, so an untouched section's concurrent edit survives).
        new_extra = dict(fresh_extra or {})
        for p in pushes:
            new_extra[p.key] = md_section_to_html(p.body)
        snap.before_update_report(doc.report_id, fresh_extra or {})
        client.update_report(doc.report_id, {"extraFields": new_extra})
        for p in pushes:
            result.pushed.append(p.path)
            _emit(on_event, f"push {_rel(root, p.path)}")
    elif pushes:  # dry-run — _guard_stale_push never runs, pushes is the plain plan list
        for p in pushes:
            result.pushed.append(p.path)
            _emit(on_event, f"would push {_rel(root, p.path)}")

    # refresh the read-only metadata mirror + section merge bases (skip in dry-run)
    if not dry_run:
        final_hashes, removed_remotely = _final_section_hashes(rdir, doc, plans)
        meta_path = rdir / REPORT_META
        text = meta_to_yaml(doc, final_hashes, removed_remotely)
        if not meta_path.exists() or meta_path.read_text(encoding="utf-8") != text:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(text, encoding="utf-8")
            result.materialized.append(meta_path)
            _emit(on_event, f"report meta {_rel(root, meta_path)}")


def _final_section_hashes(
    rdir: Path, doc: ReportDoc, plans: list[_SectionPlan]
) -> tuple[dict[str, str], set[str]]:
    """The merge base to stamp after applying, plus which keys are flagged
    ``removed_remotely``. A pushed/clean/repaired section converges on its content; a
    collision keeps the prior base (unresolved). A key that dropped out of the fresh
    fetch entirely but still has an active local narrative file keeps its last-known
    base (rather than losing it, per the note below) and is flagged so the "remote
    section gone — kept locally" skip note survives more than one sync cycle instead of
    degrading to "unknown field" after the first (F3: without this the base was only
    ever stamped for keys present in doc.sections, so it silently evaporated one cycle
    after the remote key vanished)."""
    action_by_key = {p.key: p.action for p in plans}
    hashes: dict[str, str] = {}
    _, prior, _prior_removed = read_local_meta(rdir)
    for key, section in doc.sections.items():
        action = action_by_key.get(key, "clean")
        if action == "collision":
            if key in prior:
                hashes[key] = prior[key]  # leave the base untouched until resolved
            continue
        # pull/repair converge on remote; push converges on local; clean already matches
        local_md = read_local_section(rdir, key)
        hashes[key] = section_hash(local_md) if local_md is not None else section_hash(section.body)

    removed: set[str] = set()
    for key, base in prior.items():
        if key in doc.sections or key in hashes:
            continue  # still live remotely (or already covered above)
        if read_local_section(rdir, key) is not None:
            hashes[key] = base
            removed.add(key)
    return hashes, removed
