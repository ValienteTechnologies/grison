"""Sync — reconcile the workspace with Ghostwriter, direction derived per record.

The 3-way base is ``synced.hash``. Per record: only-local-changed → **push**;
only-remote → **pull**; both → **collision** (surface both sides via an ``x.remote.md``
sidecar, never overwrite); neither → clean; converged-under-a-stale-base → repair the
hash. **Location is identity** — a file's directory fixes its Ghostwriter target
(``findings/library/`` → ``finding``, ``findings/reports/N-…/`` → ``reportedFinding`` in
report N); a file whose location disagrees with its stored id is a *move* → a new
record. grison never creates the report itself. Every remote write is snapshot-backed.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from grison.markdown import DocumentError, finding_to_markdown, markdown_to_finding
from grison.model import EvidenceGwRef, EvidenceItem, Finding
from grison.remote.gwmap import (
    content_hash,
    evidence_basename,
    finding_to_gw_fields,
    gw_pre_image,
    gw_record_to_finding,
    stamp_synced,
)
from grison.remote.snapshot import Snapshot
from grison.sinks.file_sink import slugify

if TYPE_CHECKING:
    from grison.remote.ghostwriter import GhostwriterClient

_REPORT_DIR_RE = re.compile(r"^(\d+)-")


@dataclass
class PullResult:
    written: list[Path] = field(default_factory=list)  # new or fast-forward-updated
    unchanged: list[Path] = field(default_factory=list)  # already identical
    local_ahead: list[Path] = field(default_factory=list)  # locally edited — preserved (push later)
    collisions: list[Path] = field(default_factory=list)  # changed on both sides — surfaced
    evidence_written: int = 0
    errors: list[str] = field(default_factory=list)


def _image_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _emit(on_event: Callable[[str], None] | None, msg: str) -> None:
    if on_event:
        on_event(msg)


def _stem(f: Finding, slug_counts: Counter | None = None) -> str:
    slug = slugify(f.title)
    if f.grison.gw.id is not None and (
        f.grison.tier == "instance" or (slug_counts is not None and slug_counts[slug] > 1)
    ):
        # instances always carry the id; library findings only when two remote records
        # share a title-slug — bare slugs would collapse them onto one local file
        # (silent last-writer-wins, recurring every sync).
        return f"{f.grison.gw.id}-{slug}"
    return slug


def _carry_local_only(remote_f: Finding, local_f: Finding) -> None:
    """Copy the fields grison owns locally onto a freshly-built remote finding before
    it overwrites the local file. GW has no column for ``cwe``/``tags`` and no
    update-in-place for evidence captions/friendly names, so the remote record can
    never carry them — rebuilding the file without this wipes local curation on
    every routine pull. (Consequence: a caption edited in the GW web UI after upload
    loses to a local value; grison treats un-pushable fields as locally owned.)"""
    remote_f.cwe = list(local_f.cwe)
    remote_f.tags = list(local_f.tags)
    local_ev = {e.gw.id: e for e in local_f.evidence if e.gw is not None and e.gw.id is not None}
    for entry in remote_f.evidence:
        if entry.gw is not None and entry.gw.id in local_ev:
            entry.caption = local_ev[entry.gw.id].caption
            entry.friendly_name = local_ev[entry.gw.id].friendly_name


def _scan_local(root: Path) -> dict[tuple[str, int], tuple[Path, Finding]]:
    """Index existing synced records by (gw table, gw id) — sync matches by id, not name."""
    index: dict[tuple[str, int], tuple[Path, Finding]] = {}
    for sub in ("findings/library", "findings/reports"):
        base = root / sub
        if not base.exists():
            continue
        for md in base.rglob("*.md"):
            if md.name.endswith(".remote.md"):  # collision sidecar — not a record
                continue
            if "narrative" in md.parts:  # report-narrative subtree — owned by reports.py
                continue
            try:
                f = markdown_to_finding(md.read_text(encoding="utf-8"))
            except (DocumentError, ValueError, OSError):
                continue
            if f.grison.gw.id is not None and f.grison.gw.table is not None:
                index[(f.grison.gw.table, f.grison.gw.id)] = (md, f)
    return index


def _report_dir(root: Path, report_id: int, reports: dict[int, dict]) -> Path:
    title = reports.get(report_id, {}).get("title", "report")
    return root / "findings" / "reports" / f"{report_id}-{slugify(title)}"


def _library_slug_counts(findings: list[dict]) -> Counter:
    """Title-slug multiset over the remote library — duplicate-titled findings must
    not collapse onto one local filename."""
    return Counter(slugify((rec.get("title") or "").strip() or "Untitled finding")
                   for rec in findings)


def _evidence_name_counters(
    reported: list[dict], ev_by_finding: dict[int, list[dict]]
) -> dict[int, Counter]:
    """Evidence basename multiset per *report* — every finding in a report shares one
    evidence/ directory, so filename-collision detection must span the whole report,
    not a single finding's own rows."""
    counters: dict[int, Counter] = {}
    for rec in reported:
        c = counters.setdefault(rec["reportId"], Counter())
        for row in ev_by_finding.get(rec["id"], []):
            c[evidence_basename(row)] += 1
    return counters


def pull(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool = False,
    on_event: Callable[[str], None] | None = None,
) -> PullResult:
    """Mirror Ghostwriter down into the workspace (read side of sync)."""
    result = PullResult()
    _emit(on_event, "pulling remote state from ghostwriter…")
    reports = {r["id"]: r for r in client.fetch_reports()}
    ev_by: dict[int, list[dict]] = {}
    for e in client.fetch_evidence():
        ev_by.setdefault(e["findingId"], []).append(e)
    findings = client.fetch_findings()
    reported = client.fetch_reported_findings()
    n_lib, n_rep, n_reports = len(findings), len(reported), len(reports)
    _emit(on_event, f"remote: {n_lib} library findings, {n_rep} reported, {n_reports} reports")
    local = _scan_local(root)
    lib_slugs = _library_slug_counts(findings)

    for rec in findings:
        f = gw_record_to_finding(rec, tier="library")
        _reconcile(
            result, f, local, root / "findings" / "library", None, client, root,
            slug_counts=lib_slugs, dry_run=dry_run, on_event=on_event,
        )

    ev_names = _evidence_name_counters(reported, ev_by)
    for rec in reported:
        evs = ev_by.get(rec["id"], [])
        f = gw_record_to_finding(rec, tier="instance", evidence_rows=evs,
                                 evidence_names=ev_names.get(rec["reportId"]))
        rdir = _report_dir(root, rec["reportId"], reports)
        _reconcile(result, f, local, rdir, evs, client, root, dry_run=dry_run, on_event=on_event)

    return result


def _reconcile(
    result: PullResult,
    remote: Finding,
    local: dict[tuple[str, int], tuple[Path, Finding]],
    target_dir: Path,
    ev_rows: list[dict] | None,
    client: GhostwriterClient,
    root: Path,
    *,
    slug_counts: Counter | None = None,
    dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    key = (remote.grison.gw.table, remote.grison.gw.id)
    remote_hash = content_hash(remote)
    existing = local.get(key)  # type: ignore[arg-type]

    if existing is not None:
        path, local_f = existing
        base = local_f.grison.synced.hash if local_f.grison.synced else None
        local_hash = content_hash(local_f)
        if local_hash != base:  # locally edited since last sync
            if remote_hash == base:
                result.local_ahead.append(path)  # only local moved → push later, don't clobber
            else:
                result.collisions.append(path)  # both moved → collision
            return
        if remote_hash == base:
            result.unchanged.append(path)  # clean, no-op
            return
        target_path = path  # local clean, remote moved → fast-forward pull
        _carry_local_only(remote, local_f)
    else:
        target_path = target_dir / f"{_stem(remote, slug_counts)}.md"

    if dry_run:
        result.written.append(target_path)
        _emit(on_event, f"would pull {_rel(root, target_path)}")
        return

    _download_evidence(remote, ev_rows, client, target_dir, result, on_event=on_event)
    stamp_synced(remote)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(finding_to_markdown(remote), encoding="utf-8")
    result.written.append(target_path)
    _emit(on_event, f"pull {_rel(root, target_path)}")


def _download_evidence(
    remote: Finding,
    ev_rows: list[dict] | None,
    client: GhostwriterClient,
    target_dir: Path,
    result: PullResult,
    *,
    on_event: Callable[[str], None] | None = None,
) -> None:
    if not ev_rows:
        return
    for entry in remote.evidence:
        if entry.gw is None:
            continue
        _filename, data = client.download_evidence(entry.gw.id)
        img_path = target_dir / entry.file
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(data)
        entry.gw.hash = _image_hash(data)
        result.evidence_written += 1
        _emit(on_event, f"evidence ↓ {Path(entry.file).name}")


# --- full 3-way sync (Phase 8) ----------------------------------------------


@dataclass
class SyncResult:
    pulled: list[Path] = field(default_factory=list)  # remote -> local (new or fast-forward)
    pushed: list[Path] = field(default_factory=list)  # local -> remote update
    inserted: list[Path] = field(default_factory=list)  # new/moved record created remotely
    unchanged: list[Path] = field(default_factory=list)
    repaired: list[Path] = field(default_factory=list)  # converged under a stale base
    collisions: list[Path] = field(default_factory=list)  # changed on both sides (sidecar written)
    invalid: list[Path] = field(default_factory=list)  # id set, base missing — broken link
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    evidence_up: int = 0
    evidence_down: int = 0
    evidence_deleted: int = 0
    snapshot_dir: Path | None = None
    mass_change_blocked: bool = False
    errors: list[str] = field(default_factory=list)


def _tier(loc_table: str) -> str:
    return "library" if loc_table == "finding" else "instance"


def target_from_location(root: Path, path: Path) -> tuple[str, int | None] | None:
    """Map a file to its GW target ``(table, report_id)`` — ``None`` if non-conforming.

    A reportedFinding is a *direct* child of its report dir; anything deeper (the
    ``narrative/`` report-narrative subtree, owned by :mod:`grison.remote.reports`) is
    not a finding."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) >= 3 and parts[0] == "findings" and parts[1] == "library":
        return ("finding", None)
    if len(parts) == 4 and parts[0] == "findings" and parts[1] == "reports":
        m = _REPORT_DIR_RE.match(parts[2])
        if m:
            return ("reportedFinding", int(m.group(1)))
    return None


def _location_agrees(f: Finding, loc_table: str, loc_report: int | None) -> bool:
    gw = f.grison.gw
    if gw.table != loc_table:
        return False
    return not (loc_table == "reportedFinding" and gw.report_id != loc_report)


@dataclass
class _Local:
    path: Path
    finding: Finding
    loc_table: str
    loc_report: int | None


@dataclass
class _Plan:
    action: str  # clean|push|pull|insert|collision|repair|invalid|skip
    local: _Local | None = None
    key: tuple[str, int] | None = None
    rec: dict | None = None
    remote_f: Finding | None = None
    new_path: Path | None = None
    note: str = ""


def _scan_synced(
    root: Path, result: SyncResult, *, on_event: Callable[[str], None] | None = None
) -> tuple[list[_Local], set[tuple[str, int]]]:
    """Scan synced trees → local records + the set of duplicate identities (trip-wire)."""
    locals_: list[_Local] = []
    seen: dict[tuple[str, int], Path] = {}
    dups: set[tuple[str, int]] = set()
    for sub in ("findings/library", "findings/reports"):
        base = root / sub
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            if "narrative" in md.parts:  # report-narrative subtree — owned by reports.py
                continue
            target = target_from_location(root, md)
            if target is None:
                result.skipped.append((md, "non-conforming path"))
                _emit(on_event, f"skip {_rel(root, md)}: non-conforming path")
                continue
            try:
                f = markdown_to_finding(md.read_text(encoding="utf-8"))
            except (DocumentError, ValueError, OSError) as e:
                result.errors.append(f"{md}: {e}")
                _emit(on_event, f"error {_rel(root, md)}: {e}")
                continue
            loc_table, loc_report = target
            locals_.append(_Local(md, f, loc_table, loc_report))
            if f.grison.gw.id is not None and _location_agrees(f, loc_table, loc_report):
                key = (loc_table, f.grison.gw.id)
                if key in seen:
                    dups.add(key)
                    result.skipped.append((md, f"duplicate identity {key} (also {seen[key]})"))
                    _emit(on_event, f"skip {_rel(root, md)}: duplicate identity {key}")
                else:
                    seen[key] = md
    return locals_, dups


def _relocate(f: Finding, loc_table: str, loc_report: int | None) -> Finding:
    """Rebuild a finding for insertion at its location's target (tier/table/report fixed)."""
    data = f.model_dump(mode="json")
    tier = _tier(loc_table)
    data["grison"]["tier"] = tier
    data["grison"]["gw"] = {"table": loc_table, "id": None, "report_id": loc_report}
    data["grison"].pop("synced", None)
    if tier == "library":  # library findings can't carry instance-only fields
        data["affected_entities"] = None
        data["evidence"] = []
    elif tier == "instance":
        # instance→instance move: evidence still points at the old (now orphaned) record's
        # rows — clear it so _push_evidence re-uploads to the new record instead of skipping.
        for e in data["evidence"]:
            e["gw"] = None
    return Finding.model_validate(data)


def _classify(
    lr: _Local,
    remote_index: dict[tuple[str, int], dict],
    ev_by_finding: dict[int, list[dict]],
    ev_names_by_report: dict[int, Counter],
    force_local: set[Path],
    force_remote: set[Path],
    dups: set[tuple[str, int]],
) -> _Plan:
    f = lr.finding
    gw = f.grison.gw
    agrees = _location_agrees(f, lr.loc_table, lr.loc_report)
    key = (lr.loc_table, gw.id) if (gw.id is not None and agrees) else None

    if key in dups:
        return _Plan("skip", lr, key, note="duplicate identity")

    if lr.path in force_remote:
        rec = remote_index.get(key) if key else None
        if rec:
            return _Plan("pull", lr, key, rec)
        return _Plan("skip", lr, note="--force-remote: no remote record")
    if lr.path in force_local:
        rec = remote_index.get(key) if key else None
        return _Plan("push", lr, key, rec) if rec else _Plan("insert", lr)

    if gw.id is None:
        return _Plan("insert", lr)  # new by birth
    if not agrees:
        return _Plan("insert", lr)  # moved between cells → new record at the location target
    if f.grison.synced is None or f.grison.synced.hash is None:
        return _Plan("invalid", lr, key)  # id set, agrees, no base → broken link
    rec = remote_index.get(key)
    if rec is None:
        return _Plan("skip", lr, note="remote record gone (orphan)")
    if lr.loc_table == "reportedFinding" and rec.get("reportId") != lr.loc_report:
        # the id still lives at this location locally, but Ghostwriter moved the record to
        # another report — inserting here would fork it; surface instead of duplicating.
        return _Plan(
            "skip", lr, key, rec,
            note=(
                f"moved to report {rec.get('reportId')} on Ghostwriter — delete the local "
                "file to re-pull it there, or --force-local to push it back"
            ),
        )

    base = f.grison.synced.hash
    local_hash = content_hash(f)
    ev_rows = ev_by_finding.get(rec["id"]) if lr.loc_table == "reportedFinding" else None
    ev_names = ev_names_by_report.get(rec.get("reportId")) if ev_rows else None
    remote_f = gw_record_to_finding(rec, tier=_tier(lr.loc_table), evidence_rows=ev_rows,
                                    evidence_names=ev_names)
    remote_hash = content_hash(remote_f)
    if local_hash == base and remote_hash == base:
        # clean prose, but a prior upload may have failed mid-batch, or an image's bytes
        # changed under the same filename (invisible to content_hash) → finish/reconcile it
        if lr.loc_table == "reportedFinding" and (
            _has_pending_evidence(f) or _has_stale_evidence(f, lr.path)
        ):
            return _Plan("push", lr, key, rec)
        return _Plan("clean", lr, key, rec)
    if local_hash != base and remote_hash == base:
        return _Plan("push", lr, key, rec)
    if local_hash == base and remote_hash != base:
        return _Plan("pull", lr, key, rec, remote_f=remote_f)
    if local_hash == remote_hash:
        return _Plan("repair", lr, key, rec)  # both moved but converged / stale base
    return _Plan("collision", lr, key, rec, remote_f=remote_f)


def sync(
    root: Path,
    client: GhostwriterClient,
    *,
    dry_run: bool = False,
    force_local: set[Path] | None = None,
    force_remote: set[Path] | None = None,
    mass_change_ratio: float = 0.2,
    on_event: Callable[[str], None] | None = None,
) -> SyncResult:
    """Full 3-way sync with Ghostwriter: push/pull/collision derived per record."""
    force_local = force_local or set()
    force_remote = force_remote or set()
    result = SyncResult()

    _emit(on_event, "pulling remote state from ghostwriter…")
    reports = {r["id"]: r for r in client.fetch_reports()}
    remote_index: dict[tuple[str, int], dict] = {}
    findings = client.fetch_findings()
    lib_slugs = _library_slug_counts(findings)
    for rec in findings:
        remote_index[("finding", rec["id"])] = rec
    reported = client.fetch_reported_findings()
    for rec in reported:
        remote_index[("reportedFinding", rec["id"])] = rec
    ev_by_finding: dict[int, list[dict]] = {}
    for e in client.fetch_evidence():
        ev_by_finding.setdefault(e["findingId"], []).append(e)
    ev_names_by_report = _evidence_name_counters(reported, ev_by_finding)
    n_lib, n_rep, n_reports = len(findings), len(reported), len(reports)
    _emit(on_event, f"remote: {n_lib} library findings, {n_rep} reported, {n_reports} reports")

    locals_, dups = _scan_synced(root, result, on_event=on_event)
    _emit(on_event, f"reconciling {len(locals_)} records…")

    plans: list[_Plan] = []
    matched: set[tuple[str, int]] = set()
    for lr in locals_:
        try:
            plan = _classify(lr, remote_index, ev_by_finding, ev_names_by_report,
                             force_local, force_remote, dups)
        except Exception as e:  # noqa: BLE001 — one malformed record must not abort the batch
            result.errors.append(f"{lr.path}: {e}")
            _emit(on_event, f"error {_rel(root, lr.path)}: {e}")
            # still register this identity so the remote-only pull loop below doesn't
            # treat the errored-out local record as absent and clobber it.
            f = lr.finding
            if f.grison.gw.id is not None and _location_agrees(f, lr.loc_table, lr.loc_report):
                matched.add((lr.loc_table, f.grison.gw.id))
            continue
        plans.append(plan)
        if plan.key:
            matched.add(plan.key)

    # remote-only records → pull them down (also the "vanished local → pull back" case)
    for key, rec in remote_index.items():
        if key in matched:
            continue
        try:
            table, _id = key
            tier = _tier(table)
            remote_f = gw_record_to_finding(
                rec, tier=tier, evidence_rows=ev_by_finding.get(rec["id"]),
                evidence_names=(
                    ev_names_by_report.get(rec.get("reportId")) if tier == "instance" else None
                ),
            )
            new_path = _remote_target_path(root, remote_f, rec, reports, lib_slugs)
        except Exception as e:  # noqa: BLE001 — one malformed record must not abort the batch
            result.errors.append(f"{key[0]} {key[1]}: {e}")
            _emit(on_event, f"error {key[0]} {key[1]}: {e}")
            continue
        plans.append(_Plan("pull", None, key, rec, remote_f=remote_f, new_path=new_path))

    # duplicate-move trip-wire: two files relocated to the SAME target still carrying the
    # same source id would each insert a separate remote record — stop them.
    moves: dict[tuple, list[_Plan]] = {}
    for p in plans:
        if p.action == "insert" and p.local and p.local.finding.grison.gw.id is not None:
            k = (p.local.loc_table, p.local.loc_report, p.local.finding.grison.gw.id)
            moves.setdefault(k, []).append(p)
    for k, group in moves.items():
        if len(group) > 1:
            for p in group:
                p.action = "skip"
                p.note = f"duplicate move (id {k[2]} → {k[0]}) — remove the extra copy first"

    # mass-change trip-wire: block a surprising number of remote writes
    remote_writes = [p for p in plans if p.action in ("push", "insert")]
    total = max(len(remote_index), 1)
    if not dry_run and len(remote_writes) > 5 and len(remote_writes) > mass_change_ratio * total:
        result.mass_change_blocked = True
        _emit(
            on_event,
            f"mass-change guard tripped: withholding {len(remote_writes)} remote writes",
        )
        for p in remote_writes:
            result.skipped.append((p.local.path, "mass-change guard — remote write withheld"))
            _emit(
                on_event,
                f"skip {_rel(root, p.local.path)}: mass-change guard — remote write withheld",
            )
        plans = [p for p in plans if p.action not in ("push", "insert")]

    # Persist the snapshot even if a record fails mid-batch (one bad record must not lose
    # the undo journal for writes already applied), and isolate per-record failures.
    snap = Snapshot()
    try:
        for plan in plans:
            try:
                _apply(plan, client, snap, result, ev_by_finding, ev_names_by_report, root,
                       dry_run=dry_run, on_event=on_event)
            except Exception as e:  # noqa: BLE001 — isolate one record, keep batch + snapshot
                where = plan.local.path if plan.local else plan.new_path
                result.errors.append(f"{where}: {e}")
                _emit(on_event, f"error {_rel(root, where)}: {e}")
    finally:
        if not dry_run and not snap.empty:
            when = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            result.snapshot_dir = snap.persist(when)
            _emit(on_event, f"snapshot → {_rel(root, result.snapshot_dir)}")
    return result


def _remote_target_path(
    root: Path, remote_f: Finding, rec: dict, reports: dict, lib_slugs: Counter
) -> Path:
    if remote_f.grison.tier == "library":
        return root / "findings" / "library" / f"{_stem(remote_f, lib_slugs)}.md"
    return _report_dir(root, rec["reportId"], reports) / f"{rec['id']}-{slugify(remote_f.title)}.md"


def _apply(
    plan: _Plan,
    client: GhostwriterClient,
    snap: Snapshot,
    result: SyncResult,
    ev_by_finding: dict[int, list[dict]],
    ev_names_by_report: dict[int, Counter],
    root: Path,
    *,
    dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    action, lr = plan.action, plan.local
    if action == "clean":
        result.unchanged.append(lr.path)
    elif action == "invalid":
        result.invalid.append(lr.path)
        _emit(on_event, f"broken link {_rel(root, lr.path)}")
    elif action == "skip":
        if plan.note and lr is not None:
            result.skipped.append((lr.path, plan.note))
            _emit(on_event, f"skip {_rel(root, lr.path)}: {plan.note}")
    elif action == "collision":
        if dry_run:
            _emit(on_event, f"would collision {_rel(root, lr.path)}")
        else:
            sidecar = lr.path.with_suffix(".remote.md")
            sidecar.write_text(finding_to_markdown(plan.remote_f), encoding="utf-8")
            _emit(on_event, f"collision {_rel(root, lr.path)} → sidecar written")
        result.collisions.append(lr.path)
    elif action == "repair":
        tense = "would " if dry_run else ""
        if not dry_run:
            stamp_synced(lr.finding)
            lr.path.write_text(finding_to_markdown(lr.finding), encoding="utf-8")
        result.repaired.append(lr.path)
        _emit(on_event, f"{tense}repair {_rel(root, lr.path)}")
    elif action == "pull":
        _apply_pull(plan, client, result, ev_by_finding, ev_names_by_report, root,
                    dry_run=dry_run, on_event=on_event)
    elif action == "push":
        _apply_push(plan, client, snap, result, ev_by_finding, root, dry_run=dry_run,
                     on_event=on_event)
    elif action == "insert":
        _apply_insert(plan, client, snap, result, root, dry_run=dry_run, on_event=on_event)


def _apply_pull(
    plan: _Plan, client: GhostwriterClient, result: SyncResult,
    ev_by_finding: dict[int, list[dict]], ev_names_by_report: dict[int, Counter],
    root: Path, *, dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    path = plan.new_path if plan.new_path is not None else plan.local.path
    if dry_run:
        result.pulled.append(path)
        _emit(on_event, f"would pull {_rel(root, path)}")
        return
    remote_f = plan.remote_f
    if remote_f is None:
        tier = _tier(plan.key[0])
        remote_f = gw_record_to_finding(
            plan.rec, tier=tier,
            evidence_rows=ev_by_finding.get(plan.rec["id"]),
            evidence_names=(
                ev_names_by_report.get(plan.rec.get("reportId")) if tier == "instance" else None
            ),
        )
    if plan.local is not None:
        _carry_local_only(remote_f, plan.local.finding)
    evidence_down = _download_finding_evidence(remote_f, client, path.parent, on_event=on_event)
    stamp_synced(remote_f)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(finding_to_markdown(remote_f), encoding="utf-8")
    result.pulled.append(path)  # local write succeeded — now safe to report as pulled
    _emit(on_event, f"pull {_rel(root, path)}")
    result.evidence_down += evidence_down
    _clear_sidecar(path)  # collision resolved via --force-remote


def _finalize(f: Finding, path: Path) -> None:
    """Stamp the merge base and persist — the record's identity + sync state on disk."""
    stamp_synced(f)
    path.write_text(finding_to_markdown(f), encoding="utf-8")


def _clear_sidecar(path: Path) -> None:
    path.with_suffix(".remote.md").unlink(missing_ok=True)


def _has_pending_evidence(f: Finding) -> bool:
    return any(e.gw is None or e.gw.id is None for e in f.evidence)


def _is_stale_evidence(entry: EvidenceItem, base_dir: Path) -> bool:
    """True if an already-uploaded image's on-disk bytes no longer match the hash stamped
    at last sync — a same-name byte swap that the file-set-only merge base can't see. An
    entry with no stamped hash is indeterminate (e.g. not yet downloaded) and is never
    flagged."""
    if entry.gw is None or entry.gw.id is None or entry.gw.hash is None:
        return False
    img = base_dir / entry.file
    if not img.exists():
        return False
    return _image_hash(img.read_bytes()) != entry.gw.hash


def _has_stale_evidence(f: Finding, path: Path) -> bool:
    return any(_is_stale_evidence(e, path.parent) for e in f.evidence)


def _apply_push(
    plan: _Plan, client: GhostwriterClient, snap: Snapshot, result: SyncResult,
    ev_by_finding: dict[int, list[dict]], root: Path, *, dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    lr = plan.local
    if dry_run:
        result.pushed.append(lr.path)
        _emit(on_event, f"would push {_rel(root, lr.path)}")
        return
    f = lr.finding
    pre = gw_pre_image(plan.rec, tier=_tier(lr.loc_table))
    snap.before_update(lr.loc_table, f.grison.gw.id, pre)
    fields = finding_to_gw_fields(f)
    if lr.loc_table == "finding":
        client.update_finding(f.grison.gw.id, fields)
    else:
        client.update_reported_finding(f.grison.gw.id, fields)
    result.pushed.append(lr.path)  # remote write succeeded — now safe to report as pushed
    _emit(on_event, f"push {_rel(root, lr.path)}")
    _finalize(f, lr.path)  # persist the record as synced before touching evidence
    tier_is_instance = lr.loc_table == "reportedFinding"
    remote_rows = ev_by_finding.get(f.grison.gw.id) if tier_is_instance else None
    _push_evidence(f, lr.path, client, snap, result, remote_rows, on_event=on_event)
    _clear_sidecar(lr.path)  # collision resolved via --force-local


def _apply_insert(
    plan: _Plan, client: GhostwriterClient, snap: Snapshot, result: SyncResult, root: Path,
    *, dry_run: bool, on_event: Callable[[str], None] | None = None,
) -> None:
    lr = plan.local
    if dry_run:
        result.inserted.append(lr.path)
        _emit(on_event, f"would insert {_rel(root, lr.path)}")
        return
    f = _relocate(lr.finding, lr.loc_table, lr.loc_report)
    fields = finding_to_gw_fields(f)
    if lr.loc_table == "finding":
        new_id = client.insert_finding(fields)
    else:
        new_id = client.insert_reported_finding(fields)
    snap.after_insert(lr.loc_table, new_id)
    f.grison.gw.id = new_id
    result.inserted.append(lr.path)  # insert succeeded — now safe to report as inserted
    _emit(on_event, f"insert {_rel(root, lr.path)}")
    # Persist the new id + base BEFORE evidence: if an upload then fails, the next sync
    # sees the id (no duplicate re-insert) and retries only the pending evidence.
    _finalize(f, lr.path)
    _push_evidence(f, lr.path, client, snap, result, None, on_event=on_event)


def _download_finding_evidence(
    remote_f: Finding, client: GhostwriterClient, target_dir: Path,
    *, on_event: Callable[[str], None] | None = None,
) -> int:
    count = 0
    for entry in remote_f.evidence:
        if entry.gw is None:
            continue
        _name, data = client.download_evidence(entry.gw.id)
        img = target_dir / entry.file
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(data)
        entry.gw.hash = _image_hash(data)
        count += 1
        _emit(on_event, f"evidence ↓ {Path(entry.file).name}")
    return count


def _push_evidence(
    f: Finding,
    path: Path,
    client: GhostwriterClient,
    snap: Snapshot,
    result: SyncResult,
    remote_rows: list[dict] | None,
    *,
    on_event: Callable[[str], None] | None = None,
) -> None:
    """Upload pending/stale local evidence, then delete any remote row an image no longer
    claims (removed-from-frontmatter or superseded-by-a-reupload) — both directions are
    snapshot-backed so a bad batch is fully reversible."""
    if f.grison.tier != "instance":
        return
    for entry in f.evidence:
        stale = _is_stale_evidence(entry, path.parent)
        if entry.gw is not None and entry.gw.id is not None and not stale:
            continue  # already uploaded, bytes unchanged
        img = path.parent / entry.file
        if not img.exists():
            result.errors.append(f"evidence image missing: {entry.file}")
            _emit(on_event, f"error: evidence image missing: {entry.file}")
            continue
        data = img.read_bytes()
        filename = Path(entry.file).name
        new_id = client.upload_evidence(
            finding_id=f.grison.gw.id,
            filename=filename,
            caption=entry.caption,
            friendly_name=entry.friendly_name or filename,
            file_base64=base64.b64encode(data).decode(),
        )
        snap.after_upload_evidence(new_id)
        entry.gw = EvidenceGwRef(id=new_id, hash=_image_hash(data))  # old row (if any) reaped below
        result.evidence_up += 1
        _emit(on_event, f"evidence ↑ {filename}")
        _finalize(f, path)  # persist this upload's id before attempting the next

    local_ids = {e.gw.id for e in f.evidence if e.gw is not None and e.gw.id is not None}
    for row in remote_rows or []:
        if row["id"] in local_ids:
            continue  # still claimed by a local entry
        filename, data = client.download_evidence(row["id"])  # pre-image, for the undo journal
        snap.before_delete_evidence(
            row["id"],
            f.grison.gw.id,
            filename,
            row.get("caption") or "",
            row.get("friendlyName") or filename,
            base64.b64encode(data).decode(),
        )
        client.delete_evidence(row["id"])
        result.evidence_deleted += 1
        _emit(on_event, f"evidence ✕ {filename}")
