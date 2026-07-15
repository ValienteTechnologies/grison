"""Sync — reconcile the workspace with Ghostwriter, direction derived per record.

The 3-way base is ``synced.hash``. Per record: only-local-changed → **push**;
only-remote → **pull**; both → **collision** (surface both sides via an ``x.remote.md``
sidecar, never overwrite); neither → clean; converged-under-a-stale-base → repair the
hash. **Location is identity** — a file's directory fixes its Ghostwriter target
(``findings/library/`` → ``finding``, ``findings/reports/N-…/`` → ``reportedFinding`` in
report N); a file whose location disagrees with its stored id is a *move*. A tier
change (library ↔ instance) makes a new record, since the two live in different GW
tables; a same-table cross-report move (``reportedFinding`` → a different report, no
other local file still claiming the old id) **reparents the existing record** instead
of forking a duplicate. grison never creates the report itself. Every remote write is
snapshot-backed.
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

from grison.markdown import (
    DocumentError,
    extract_gw_identity,
    finding_to_markdown,
    markdown_to_finding,
)
from grison.model import (
    EvidenceGwRef,
    EvidenceItem,
    Finding,
    check_finding_type_drift,
    check_severity_drift,
)
from grison.remote.gwmap import (
    content_hash,
    evidence_basename,
    evidence_meta_hash,
    finding_to_gw_fields,
    finding_to_gw_tags,
    gw_pre_image,
    gw_record_to_finding,
    stamp_synced,
)
from grison.remote.snapshot import Snapshot
from grison.sinks.file_sink import slugify
from grison.state import StateStore, hydrate_finding, persist_finding

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


def _entry_meta(e: EvidenceItem) -> str:
    return evidence_meta_hash(e.caption, e.friendly_name, e.description)


def _row_meta(row: dict) -> str:
    return evidence_meta_hash(
        row.get("caption") or "", row.get("friendlyName") or "", row.get("description") or ""
    )


def _classify_evidence_meta(base: str | None, local: str, remote: str) -> str:
    """Per-image 3-way classification for caption/friendly_name/description — mirrors
    the record-level base/local/remote logic in :func:`_classify`. Callers check
    ``local == remote`` (converged, regardless of base) before calling this."""
    if remote == base:
        return "local_ahead"
    if local == base:
        return "remote_ahead"
    return "collision"


def _surface_remote_losses(
    result: SyncResult, root: Path, path: Path, losses: list[str]
) -> None:
    """Drain buffered on_loss messages (gwmap html->md, tagged with field name) into
    ``result.warnings`` — called only where ``remote_f`` is actually written to disk
    (a pull or a collision sidecar), never for a build that was only used to compare
    hashes, so a permanently-unconvertible remote construct doesn't re-warn every sync."""
    for msg in losses:
        result.warnings.append(f"{_rel(root, path)}: {msg}")


def _evidence_meta_collision_msg(rel_path: str, evfile: str) -> str:
    """Shared wording for a per-image caption/friendly_name/description collision —
    used on both the pull side (:func:`_reconcile`, :func:`_apply_pull`) and the push
    side (:func:`_reconcile_evidence_meta`), since it's the same 3-way outcome
    surfacing through two different code paths."""
    return (
        f"{rel_path}: evidence metadata collision on {evfile} — local "
        "and remote both changed caption/friendly_name/description; resolve manually"
    )


def _carry_local_only(remote_f: Finding, local_f: Finding, *, forced: bool = False) -> list[str]:
    """Reconcile per-image evidence metadata (caption/friendly_name/description) onto a
    freshly-built remote finding before it overwrites the local file — 3-way per image,
    keyed on ``EvidenceGwRef.meta`` (Track 1b), the same base/local/remote shape
    :func:`_classify` uses for the whole record. These fields sit outside
    ``content_hash`` (GW's evidence API predates a bulk record-level update), so each
    image tracks its own tiny merge base instead. ``cwe``/``tags`` are NOT carried here
    (Track 1a made them sync two-way via GW's tag mechanism).

    Returns the evidence files where local and remote changed AND disagree with each
    other — a genuine collision the caller surfaces (never a silent winner). When
    ``forced`` (this pull was a ``--force-remote`` resolution), remote unconditionally
    wins for every entry, matching the force semantics used at the whole-record level.
    """
    local_ev = {e.gw.id: e for e in local_f.evidence if e.gw is not None and e.gw.id is not None}
    collided: list[str] = []
    for entry in remote_f.evidence:
        if entry.gw is None or entry.gw.id not in local_ev:
            continue
        local_e = local_ev[entry.gw.id]
        base = local_e.gw.meta if local_e.gw is not None else None
        remote_meta = entry.gw.meta if entry.gw.meta is not None else _entry_meta(entry)
        local_meta = _entry_meta(local_e)
        if local_meta == remote_meta:
            entry.gw.meta = remote_meta  # converged (regardless of base) — restamp, no drift left
            continue
        outcome = (
            "remote_ahead" if forced else _classify_evidence_meta(base, local_meta, remote_meta)
        )
        if outcome == "remote_ahead":
            entry.gw.meta = remote_meta  # entry already carries remote's fresh values
            continue
        # local_ahead or collision: preserve local's values; leave the base at its old
        # (stale) value so a later sync still recognizes the divergence — local_ahead
        # eventually gets pushed, collision keeps surfacing — instead of silently
        # discarding either side.
        entry.caption = local_e.caption
        entry.friendly_name = local_e.friendly_name
        entry.description = local_e.description
        entry.gw.meta = base
        if outcome == "collision":
            collided.append(entry.file)
    return collided


def _scan_local(root: Path) -> dict[tuple[str, int], tuple[Path, Finding]]:
    """Index existing synced records by (gw table, gw id) — sync matches by id, not name."""
    index: dict[tuple[str, int], tuple[Path, Finding]] = {}
    store = StateStore(root)
    for sub in ("findings/library", "findings/reports"):
        base = root / sub
        if not base.exists():
            continue
        for md in base.rglob("*.md"):
            if md.name.endswith(".remote.md"):  # collision sidecar — not a record
                continue
            if "narrative" in md.parts:  # report-narrative subtree — owned by reports.py
                continue
            target = target_from_location(root, md)
            tier = _tier(target[0]) if target is not None else None
            try:
                f = markdown_to_finding(md.read_text(encoding="utf-8"), tier=tier)
            except (DocumentError, ValueError, OSError):
                continue
            hydrate_finding(store, f)
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
    tag_map = client.fetch_tag_map()
    findings = client.fetch_findings()
    reported = client.fetch_reported_findings()
    n_lib, n_rep, n_reports = len(findings), len(reported), len(reports)
    _emit(on_event, f"remote: {n_lib} library findings, {n_rep} reported, {n_reports} reports")
    local = _scan_local(root)
    lib_slugs = _library_slug_counts(findings)

    for rec in findings:
        f = gw_record_to_finding(rec, tier="library", tags=tag_map.get(("finding", rec["id"])))
        _reconcile(
            result, f, local, root / "findings" / "library", None, client, root,
            slug_counts=lib_slugs, dry_run=dry_run, on_event=on_event,
        )

    ev_names = _evidence_name_counters(reported, ev_by)
    for rec in reported:
        evs = ev_by.get(rec["id"], [])
        f = gw_record_to_finding(rec, tier="instance", evidence_rows=evs,
                                 evidence_names=ev_names.get(rec["reportId"]),
                                 tags=tag_map.get(("reportedFinding", rec["id"])))
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
        collided = _carry_local_only(remote, local_f)
        for evfile in collided:
            msg = _evidence_meta_collision_msg(_rel(root, target_path), evfile)
            result.errors.append(msg)
            _emit(on_event, msg)
    else:
        target_path = target_dir / f"{_stem(remote, slug_counts)}.md"

    if dry_run:
        result.written.append(target_path)
        _emit(on_event, f"would pull {_rel(root, target_path)}")
        return

    local_hashes = _stamped_hashes(local_f) if existing is not None else None
    _download_evidence(
        remote, ev_rows, client, target_dir, result, local_hashes=local_hashes, on_event=on_event
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _finalize(remote, target_path, root)
    result.written.append(target_path)
    _emit(on_event, f"pull {_rel(root, target_path)}")


def _stamped_hashes(f: Finding) -> dict[int, str]:
    """``{gw.id: gw.hash}`` for entries already carrying a stamped image hash — the
    prior local state a re-pull can compare against to skip a redundant download
    (evidence bytes are immutable per id via the GW API — a same-id download can never
    return different bytes, verified fact)."""
    return {
        e.gw.id: e.gw.hash
        for e in f.evidence
        if e.gw is not None and e.gw.id is not None and e.gw.hash is not None
    }


def _evidence_unchanged(base_dir: Path, entry: EvidenceItem, local_hashes: dict[int, str]) -> bool:
    if entry.gw is None or entry.gw.id is None:
        return False
    prior = local_hashes.get(entry.gw.id)
    if prior is None:
        return False
    img = base_dir / entry.file
    return img.exists() and _image_hash(img.read_bytes()) == prior


def _download_evidence(
    remote: Finding,
    ev_rows: list[dict] | None,
    client: GhostwriterClient,
    target_dir: Path,
    result: PullResult,
    *,
    local_hashes: dict[int, str] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> None:
    if not ev_rows:
        return
    local_hashes = local_hashes or {}
    for entry in remote.evidence:
        if entry.gw is None:
            continue
        if _evidence_unchanged(target_dir, entry, local_hashes):
            entry.gw.hash = local_hashes[entry.gw.id]  # unchanged — keep the stamped hash
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
    corrupt: list[tuple[Path, str]] = field(default_factory=list)  # fails parse/validation
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    evidence_up: int = 0
    evidence_down: int = 0
    evidence_deleted: int = 0
    snapshot_dir: Path | None = None
    mass_change_blocked: bool = False
    errors: list[str] = field(default_factory=list)
    # non-fatal: canonicalized/dropped constructs (converter on_loss), recomputed cvss
    # scores, etc — surfaced but never flip the exit code by themselves.
    warnings: list[str] = field(default_factory=list)


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
    # --force-local/--force-remote: evidence 3-way picks a side, not a collision
    forced: bool = False
    # on_loss messages from building remote_f (gwmap html->md) — only surfaced to
    # result.warnings when this plan actually writes remote_f to disk (pull/collision),
    # never for a plan that only used remote_f for hash comparison (push/clean/repair),
    # so a permanently-unconvertible remote construct doesn't re-warn every sync.
    remote_losses: list[str] = field(default_factory=list)


def _scan_synced(
    root: Path, result: SyncResult, *, on_event: Callable[[str], None] | None = None
) -> tuple[list[_Local], set[tuple[str, int]], set[tuple[str, int]], set[tuple[str, int]]]:
    """Scan synced trees → local records + the set of duplicate identities (trip-wire) +
    identities claimed by corrupt files + identities owned by a location-agreeing file.

    Returns ``(locals, dups, claimed, agreeing)``:

    * ``dups`` — an identity two *agreeing* local files both claim (existing trip-wire).
    * ``claimed`` — identities pulled from the raw frontmatter of a file that failed
      full parse/validation (corrupt-file guard, gw-pull F1): even though the file
      itself can't be trusted, its remote identity still is, so the remote-only pull
      loop below must not re-materialize a fresh copy over the broken file.
    * ``agreeing`` — ``set(seen)`` — every identity legitimately owned by a file whose
      location agrees with its own ``grison.gw`` fields, used by :func:`_classify` to
      tell a genuine cross-report move (no other local claimant) from a copy (the
      original still lives at its home location).
    """
    locals_: list[_Local] = []
    seen: dict[tuple[str, int], Path] = {}
    dups: set[tuple[str, int]] = set()
    claimed: set[tuple[str, int]] = set()
    store = StateStore(root)
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
            loc_table, loc_report = target
            try:
                text = md.read_text(encoding="utf-8")
            except OSError as e:
                result.corrupt.append((md, str(e)))
                _emit(on_event, f"corrupt {_rel(root, md)}: {e}")
                continue
            try:
                f = markdown_to_finding(text, tier=_tier(loc_table))
            except (DocumentError, ValueError) as e:
                identity = extract_gw_identity(text)
                if identity is not None:
                    claimed.add(identity)
                result.corrupt.append((md, str(e)))
                _emit(on_event, f"corrupt {_rel(root, md)}: {e}")
                continue
            hydrate_finding(store, f)
            locals_.append(_Local(md, f, loc_table, loc_report))
            if f.grison.gw.id is not None and _location_agrees(f, loc_table, loc_report):
                key = (loc_table, f.grison.gw.id)
                if key in seen:
                    dups.add(key)
                    result.skipped.append((md, f"duplicate identity {key} (also {seen[key]})"))
                    _emit(on_event, f"skip {_rel(root, md)}: duplicate identity {key}")
                else:
                    seen[key] = md
    return locals_, dups, claimed, set(seen)


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
    tag_map: dict[tuple[str, int], list[str]],
    force_local: set[Path],
    force_remote: set[Path],
    dups: set[tuple[str, int]],
    agreeing: set[tuple[str, int]],
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
            return _Plan("pull", lr, key, rec, forced=True)
        return _Plan("skip", lr, note="--force-remote: no remote record")
    if lr.path in force_local:
        rec = remote_index.get(key) if key else None
        return _Plan("push", lr, key, rec, forced=True) if rec else _Plan("insert", lr)

    if gw.id is None:
        return _Plan("insert", lr)  # new by birth
    if not agrees:
        if lr.loc_table == "finding" and (f.evidence or f.affected_entities):
            # library findings can't carry instance-only fields — refuse the move instead
            # of _relocate silently nulling them and orphaning the remote evidence rows
            # (F3-tier-relocate-wipes-evidence). The untouched remote record is unaffected
            # and re-pulls at its rightful location on this same sync.
            return _Plan(
                "skip", lr,
                note="library findings can't carry evidence/affected_entities — remove them first",
            )
        move_key = (lr.loc_table, gw.id)
        if (
            gw.table == "reportedFinding"
            and lr.loc_table == "reportedFinding"
            and move_key not in agreeing
            and f.grison.synced is not None
            and f.grison.synced.hash is not None
        ):
            # same record, only report_id disagrees, and no other local file still
            # claims this id at its home location → a genuine cross-report move
            # (gw-pull F2): reparent via push (finding_to_gw_fields already sends the
            # new reportId), not a fresh insert that would fork a duplicate remotely.
            # A file still present at the old location (a copy, not a move) keeps the
            # old insert-a-new-record behavior via the ``move_key not in agreeing`` guard.
            rec = remote_index.get(move_key)
            if rec is not None:
                gw.report_id = lr.loc_report
                return _Plan("push", lr, move_key, rec)
        return _Plan("insert", lr)  # moved between cells → new record at the location target
    if f.grison.synced is None or f.grison.synced.hash is None:
        return _Plan("invalid", lr, key)  # id set, agrees, no base → broken link
    renamed = _renamed_evidence_files(f)
    if renamed:
        # F6: evidence filenames mirror GW's server-managed storage path — a local rename
        # can't itself be pushed. Caught here, at classify time, before any push/pull.
        return _Plan(
            "skip", lr, key,
            note=(
                "evidence renamed locally (" + ", ".join(renamed) + ") — evidence filenames "
                "mirror Ghostwriter storage; delete and re-add the image instead of renaming"
            ),
        )
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
    # buffered, not surfaced yet: this build is needed for the hash comparison below on
    # EVERY sync regardless of outcome, so an on_loss firing here must not become a
    # result.warnings entry unless the outcome actually writes remote_f to disk (pull/
    # collision) — see _Plan.remote_losses.
    losses: list[str] = []
    remote_f = gw_record_to_finding(rec, tier=_tier(lr.loc_table), evidence_rows=ev_rows,
                                    evidence_names=ev_names, tags=tag_map.get(key),
                                    on_loss=losses.append)
    remote_hash = content_hash(remote_f)
    if local_hash == base and remote_hash == base:
        # clean prose, but a prior upload may have failed mid-batch, an image's bytes
        # changed under the same filename, one vanished from disk, or its caption/
        # friendly_name/description drifted (none of these move content_hash) → push to
        # finish/reconcile the evidence side
        if lr.loc_table == "reportedFinding" and _evidence_needs_push(f, lr.path, ev_rows):
            return _Plan("push", lr, key, rec)
        return _Plan("clean", lr, key, rec)
    if local_hash != base and remote_hash == base:
        return _Plan("push", lr, key, rec)
    if local_hash == base and remote_hash != base:
        return _Plan("pull", lr, key, rec, remote_f=remote_f, remote_losses=losses)
    if local_hash == remote_hash:
        # both moved but converged / stale base — still check evidence-only drift, same as
        # the clean branch above, since it's invisible to content_hash either way
        if lr.loc_table == "reportedFinding" and _evidence_needs_push(f, lr.path, ev_rows):
            return _Plan("push", lr, key, rec)
        return _Plan("repair", lr, key, rec)
    return _Plan("collision", lr, key, rec, remote_f=remote_f, remote_losses=losses)


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

    # Severity/finding-type tripwire: grison.model.enums hardcodes the GW lookup-table
    # gw_ids at import time (never re-derived per instance) — verify them against this
    # instance before touching any finding, so a drifted/unknown row aborts loudly
    # instead of silently mis-mapping every severity/finding-type synced afterward.
    check_severity_drift(client.fetch_finding_severities())
    check_finding_type_drift(client.fetch_finding_types())

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
    tag_map = client.fetch_tag_map()
    n_lib, n_rep, n_reports = len(findings), len(reported), len(reports)
    _emit(on_event, f"remote: {n_lib} library findings, {n_rep} reported, {n_reports} reports")

    locals_, dups, claimed, agreeing = _scan_synced(root, result, on_event=on_event)
    _emit(on_event, f"reconciling {len(locals_)} records…")

    plans: list[_Plan] = []
    # identities claimed by a corrupt file's raw frontmatter (gw-pull F1) — the file
    # itself never became a _Local, so seed `matched` with them up front rather than
    # only adding to it as plans are classified below.
    matched: set[tuple[str, int]] = set(claimed)
    for lr in locals_:
        try:
            plan = _classify(lr, remote_index, ev_by_finding, ev_names_by_report, tag_map,
                             force_local, force_remote, dups, agreeing)
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
            losses: list[str] = []
            remote_f = gw_record_to_finding(
                rec, tier=tier, evidence_rows=ev_by_finding.get(rec["id"]),
                evidence_names=(
                    ev_names_by_report.get(rec.get("reportId")) if tier == "instance" else None
                ),
                tags=tag_map.get(key), on_loss=losses.append,
            )
            new_path = _remote_target_path(root, remote_f, rec, reports, lib_slugs)
        except Exception as e:  # noqa: BLE001 — one malformed record must not abort the batch
            result.errors.append(f"{key[0]} {key[1]}: {e}")
            _emit(on_event, f"error {key[0]} {key[1]}: {e}")
            continue
        plans.append(
            _Plan("pull", None, key, rec, remote_f=remote_f, new_path=new_path,
                 remote_losses=losses)
        )

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

    # mass-change trip-wire: block a surprising number of remote writes, and — since
    # pull-after-push makes the whole synced corpus's bases converter-derived, so any
    # future converter behavior change would otherwise mass-pull-overwrite unguarded —
    # overwrite-pulls (an existing local file about to be replaced) too. A new-file pull
    # (plan.local is None) is a non-destructive creation and never counts or gets held.
    guarded = [
        p for p in plans
        if p.action in ("push", "insert") or (p.action == "pull" and p.local is not None)
    ]
    total = max(len(remote_index), 1)
    if not dry_run and len(guarded) > 5 and len(guarded) > mass_change_ratio * total:
        result.mass_change_blocked = True
        _emit(
            on_event,
            f"mass-change guard tripped: withholding {len(guarded)} remote writes/overwrite-pulls",
        )
        for p in guarded:
            note = (
                "mass-change guard — overwrite pull withheld"
                if p.action == "pull"
                else "mass-change guard — remote write withheld"
            )
            result.skipped.append((p.local.path, note))
            _emit(on_event, f"skip {_rel(root, p.local.path)}: {note}")
        plans = [
            p for p in plans
            if not (p.action in ("push", "insert") or (p.action == "pull" and p.local is not None))
        ]

    # Persist the snapshot even if a record fails mid-batch (one bad record must not lose
    # the undo journal for writes already applied), and isolate per-record failures.
    snap = Snapshot()
    try:
        for plan in plans:
            try:
                _apply(plan, client, snap, result, ev_by_finding, ev_names_by_report, tag_map,
                       root, dry_run=dry_run, on_event=on_event)
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
    tag_map: dict[tuple[str, int], list[str]],
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
            _surface_remote_losses(result, root, sidecar, plan.remote_losses)
        result.collisions.append(lr.path)
    elif action == "repair":
        tense = "would " if dry_run else ""
        if not dry_run:
            _finalize(lr.finding, lr.path, root)
        result.repaired.append(lr.path)
        _emit(on_event, f"{tense}repair {_rel(root, lr.path)}")
    elif action == "pull":
        _apply_pull(plan, client, result, ev_by_finding, ev_names_by_report, tag_map, root,
                    dry_run=dry_run, on_event=on_event)
    elif action == "push":
        _apply_push(plan, client, snap, result, ev_by_finding, tag_map, root, dry_run=dry_run,
                     on_event=on_event)
    elif action == "insert":
        _apply_insert(plan, client, snap, result, root, dry_run=dry_run, on_event=on_event)


def _apply_pull(
    plan: _Plan, client: GhostwriterClient, result: SyncResult,
    ev_by_finding: dict[int, list[dict]], ev_names_by_report: dict[int, Counter],
    tag_map: dict[tuple[str, int], list[str]],
    root: Path, *, dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    path = plan.new_path if plan.new_path is not None else plan.local.path
    if dry_run:
        result.pulled.append(path)
        _emit(on_event, f"would pull {_rel(root, path)}")
        return
    remote_f = plan.remote_f
    losses = plan.remote_losses
    if remote_f is None:
        tier = _tier(plan.key[0])
        losses = []
        remote_f = gw_record_to_finding(
            plan.rec, tier=tier,
            evidence_rows=ev_by_finding.get(plan.rec["id"]),
            evidence_names=(
                ev_names_by_report.get(plan.rec.get("reportId")) if tier == "instance" else None
            ),
            tags=tag_map.get(plan.key), on_loss=losses.append,
        )
    local_hashes = None
    if plan.local is not None:
        # --force-remote (plan.forced) picks remote unconditionally for every image,
        # matching the record-level force semantics; otherwise a genuine per-image
        # 3-way — see _carry_local_only.
        collided = _carry_local_only(remote_f, plan.local.finding, forced=plan.forced)
        for evfile in collided:
            msg = _evidence_meta_collision_msg(_rel(root, path), evfile)
            result.errors.append(msg)
            _emit(on_event, msg)
        local_hashes = _stamped_hashes(plan.local.finding)
    evidence_down = _download_finding_evidence(
        remote_f, client, path.parent, local_hashes=local_hashes, on_event=on_event
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _finalize(remote_f, path, root)
    result.pulled.append(path)  # local write succeeded — now safe to report as pulled
    _emit(on_event, f"pull {_rel(root, path)}")
    result.evidence_down += evidence_down
    _surface_remote_losses(result, root, path, losses)
    _clear_sidecar(path)  # collision resolved via --force-remote


def _finalize(f: Finding, path: Path, root: Path) -> None:
    """Stamp the merge base, persist it to the state store, and write the file. Identity
    stays in the file; the base (and evidence bookkeeping) now lives in ``.grison/state/``
    — this is the single place that keeps the two in sync on every stamp/finalize."""
    stamp_synced(f)
    persist_finding(StateStore(root), f)
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


def _is_missing_evidence(entry: EvidenceItem, base_dir: Path) -> bool:
    """True if an already-uploaded image's local file is gone entirely — deleted, moved
    out from under grison, whatever. content_hash's file-set-only view can't see this
    (the filename is still in the frontmatter), so without this check the record
    classifies 'clean' forever and the deletion is never noticed (F2)."""
    if entry.gw is None or entry.gw.id is None:
        return False
    return not (base_dir / entry.file).exists()


def _has_missing_evidence(f: Finding, path: Path) -> bool:
    return any(_is_missing_evidence(e, path.parent) for e in f.evidence)


def _has_evidence_meta_drift(f: Finding, ev_rows: list[dict] | None) -> bool:
    """True if any already-uploaded image's caption/friendly_name/description differs
    from Ghostwriter's current row — invisible to content_hash, so a record can be
    otherwise byte-for-byte clean while still owing a metadata push/pull (gw-push-2)."""
    if not ev_rows:
        return False
    remote_by_id = {r["id"]: r for r in ev_rows}
    for e in f.evidence:
        if e.gw is None or e.gw.id is None:
            continue
        rec = remote_by_id.get(e.gw.id)
        if rec is not None and _entry_meta(e) != _row_meta(rec):
            return True
    return False


def _evidence_needs_push(f: Finding, path: Path, ev_rows: list[dict] | None) -> bool:
    """Any per-image reason a 'clean'/'repair' record still needs a push pass: an upload
    never completed, bytes changed or vanished under an unchanged filename, or metadata
    drifted — all invisible to content_hash by design."""
    return (
        _has_pending_evidence(f)
        or _has_stale_evidence(f, path)
        or _has_missing_evidence(f, path)
        or _has_evidence_meta_drift(f, ev_rows)
    )


def _renamed_evidence_files(f: Finding) -> list[str]:
    """Evidence entries whose local ``file`` basename no longer matches the basename
    grison stamped at upload/pull time (F6) — a local rename GW's server-managed
    storage path can't reflect. Purely local: no remote fetch needed."""
    return [
        e.file
        for e in f.evidence
        if e.gw is not None
        and e.gw.id is not None
        and e.gw.basename is not None
        and Path(e.file).name != e.gw.basename
    ]


def _push_tags(
    f: Finding,
    table: str,
    client: GhostwriterClient,
    snap: Snapshot,
    remote_tags: list[str],
) -> bool:
    """Push the cwe+tags projection via ``setTags``, skipping the call when the remote
    side already carries exactly this set (order-insensitive — GW tags are a set, and
    a same-content call would be a no-op mutation). Returns whether ``setTags`` was
    actually called — the caller uses this to decide whether the canonical tag state
    needs a re-fetch (taggit case-folds/reuses an existing tag row, so the just-sent
    list is never trustworthy) or whether the batch-start, already-remote-derived
    ``remote_tags`` is still accurate."""
    desired = finding_to_gw_tags(f)
    if sorted(desired) == sorted(remote_tags):
        return False
    snap.before_set_tags(table, f.grison.gw.id, remote_tags)
    client.set_tags(f.grison.gw.id, table, desired)
    return True


def _finalize_canonical(shell: Finding, f: Finding, path: Path, root: Path) -> None:
    """Persist the canonical, converter-rebuilt ``shell`` (never the pre-mutation local
    ``f``, whose prose is the whole echo bug) — but with evidence always taken from
    ``f``'s live, in-place-mutated list, never from the shell's own rebuild (built with
    ``evidence_rows=None``). Rebuilding evidence from the shell would silently drop
    pending, not-yet-uploaded entries from the file — a data loss invisible to
    ``content_hash``. Re-assigning on every call (rather than once) keeps this correct
    even if a future change ever reassigns ``f.evidence`` outright instead of mutating
    entries in place."""
    shell.evidence = f.evidence
    _finalize(shell, path, root)


def _apply_push(
    plan: _Plan, client: GhostwriterClient, snap: Snapshot, result: SyncResult,
    ev_by_finding: dict[int, list[dict]], tag_map: dict[tuple[str, int], list[str]],
    root: Path, *, dry_run: bool,
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
        rec_after = client.update_finding(f.grison.gw.id, fields)
    else:
        rec_after = client.update_reported_finding(f.grison.gw.id, fields)
    # The remote write landed — report + clear the sidecar right now. Everything past
    # this point (tag re-fetch, canonical rebuild, evidence) can fail independently
    # without undoing a write Ghostwriter already committed.
    result.pushed.append(lr.path)
    _emit(on_event, f"push {_rel(root, lr.path)}")
    _clear_sidecar(lr.path)  # collision resolved via --force-local, or just a routine push

    remote_tags = tag_map.get((lr.loc_table, f.grison.gw.id), [])
    pushed_tags = _push_tags(f, lr.loc_table, client, snap, remote_tags)
    # taggit is case-insensitive and reuses an existing case-variant tag row as-is —
    # never trust the list just sent; re-fetch what actually landed. If nothing was
    # pushed, remote_tags (this batch's fetch_tag_map entry) is already remote-derived.
    canonical_tags = (
        client.fetch_tags_for(lr.loc_table, f.grison.gw.id) if pushed_tags else remote_tags
    )

    def finalize(path: Path) -> None:
        _finalize_canonical(shell, f, path, root)

    try:
        shell = gw_record_to_finding(
            rec_after, tier=_tier(lr.loc_table), tags=canonical_tags, evidence_rows=None,
        )
    except (ValueError, KeyError, TypeError) as e:
        result.errors.append(
            f"{_rel(root, lr.path)}: pushed to ghostwriter but the returned record could "
            f"not be re-canonicalized — re-run sync to reclassify ({e})"
        )
        return  # local restamp skipped — falling back to the local finding would silently
        # reintroduce the echo; next sync self-heals as a repair or collision.

    finalize(lr.path)  # persist the record as synced before touching evidence
    tier_is_instance = lr.loc_table == "reportedFinding"
    remote_rows = ev_by_finding.get(f.grison.gw.id) if tier_is_instance else None
    _push_evidence(
        f, lr.path, client, snap, result, remote_rows, root, finalize=finalize, on_event=on_event
    )


def _apply_insert(
    plan: _Plan, client: GhostwriterClient, snap: Snapshot, result: SyncResult, root: Path,
    *, dry_run: bool, on_event: Callable[[str], None] | None = None,
) -> None:
    lr = plan.local
    if dry_run:
        result.inserted.append(lr.path)
        _emit(on_event, f"would insert {_rel(root, lr.path)}")
        return
    old_table, old_id = lr.finding.grison.gw.table, lr.finding.grison.gw.id
    f = _relocate(lr.finding, lr.loc_table, lr.loc_report)
    fields = finding_to_gw_fields(f)
    if lr.loc_table == "finding":
        rec_after = client.insert_finding(fields)
    else:
        rec_after = client.insert_reported_finding(fields)
    new_id = rec_after["id"]
    snap.after_insert(lr.loc_table, new_id)
    f.grison.gw.id = new_id
    if old_id is not None:
        # insert-from-move (tier change or cross-cell relocation): the source identity's
        # store entry is now orphaned — no local file claims it any more — so drop it
        # rather than leaving a stale base nothing will ever read again.
        StateStore(root).delete_finding(old_table, old_id)
    result.inserted.append(lr.path)  # insert succeeded — now safe to report as inserted
    _emit(on_event, f"insert {_rel(root, lr.path)}")

    # brand-new record — no remote tags yet, so a push always means canonical tags need
    # the re-fetch (case-fold/reuse, same as _apply_push); no push (no local tags/cwe
    # authored) means the canonical set is just the empty list.
    pushed_tags = _push_tags(f, lr.loc_table, client, snap, [])
    canonical_tags = client.fetch_tags_for(lr.loc_table, new_id) if pushed_tags else []

    def finalize(path: Path) -> None:
        _finalize_canonical(shell, f, path, root)

    try:
        shell = gw_record_to_finding(
            rec_after, tier=_tier(lr.loc_table), tags=canonical_tags, evidence_rows=None,
        )
    except (ValueError, KeyError, TypeError) as e:
        result.errors.append(
            f"{_rel(root, lr.path)}: inserted into ghostwriter (id {new_id}) but the "
            f"returned record could not be re-canonicalized — re-run sync to reclassify ({e})"
        )
        # Persist the new id WITHOUT a base (bypassing stamp_synced) — unlike a push,
        # nothing on disk carried this identity before, so writing nothing at all would
        # let the next sync re-insert a duplicate. persist_finding still runs (base=None
        # since grison.synced is unset) so any uploaded-evidence ids stay recorded. Next
        # sync instead sees a broken link (id set, no base) and surfaces it loudly rather
        # than silently reintroducing the echo by stamping a base off pre-canonicalization
        # local content.
        persist_finding(StateStore(root), f)
        lr.path.write_text(finding_to_markdown(f), encoding="utf-8")
        return

    # Persist the new id + base BEFORE evidence: if an upload then fails, the next sync
    # sees the id (no duplicate re-insert) and retries only the pending evidence.
    finalize(lr.path)
    _push_evidence(
        f, lr.path, client, snap, result, None, root, finalize=finalize, on_event=on_event
    )


def _download_finding_evidence(
    remote_f: Finding, client: GhostwriterClient, target_dir: Path,
    *, local_hashes: dict[int, str] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> int:
    count = 0
    local_hashes = local_hashes or {}
    for entry in remote_f.evidence:
        if entry.gw is None:
            continue
        if _evidence_unchanged(target_dir, entry, local_hashes):
            entry.gw.hash = local_hashes[entry.gw.id]
            continue
        _name, data = client.download_evidence(entry.gw.id)
        img = target_dir / entry.file
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(data)
        entry.gw.hash = _image_hash(data)
        count += 1
        _emit(on_event, f"evidence ↓ {Path(entry.file).name}")
    return count


def _reconcile_evidence_meta(
    f: Finding,
    entry: EvidenceItem,
    row: dict,
    path: Path,
    client: GhostwriterClient,
    snap: Snapshot,
    result: SyncResult,
    root: Path,
    *,
    finalize: Callable[[Path], None],
    on_event: Callable[[str], None] | None = None,
) -> None:
    """Per-image 3-way reconcile for caption/friendly_name/description when an entry's
    bytes are unchanged (gw-push-2) — this is the push-side counterpart of
    :func:`_carry_local_only`. local-only changed → push via ``update_evidence``,
    sending only the fields that actually differ (never resend an untouched
    ``friendly_name`` — that field alone fires GW's ``{{.Name}}``-ref-rewrite trigger);
    remote-only changed → adopt into local; both → collision, surfaced exactly like a
    body collision (never a silent winner — local's file is left untouched so nothing
    on disk is lost, but the stale base keeps flagging it every sync until resolved).
    ``finalize`` is the push-side canonical persist (see :func:`_finalize_canonical`),
    passed down so the on-disk file stays the converter-canonical shell throughout."""
    assert entry.gw is not None and entry.gw.id is not None
    base = entry.gw.meta
    local_meta = _entry_meta(entry)
    remote_meta = _row_meta(row)
    if local_meta == remote_meta:
        if entry.gw.meta != remote_meta:  # e.g. base predates Track 1b — restamp, no drift
            entry.gw.meta = remote_meta
            finalize(path)
        return
    outcome = _classify_evidence_meta(base, local_meta, remote_meta)
    if outcome == "local_ahead":
        fields: dict = {}
        if entry.caption != (row.get("caption") or ""):
            fields["caption"] = entry.caption
        if entry.friendly_name != (row.get("friendlyName") or ""):
            fields["friendlyName"] = entry.friendly_name
        if entry.description != (row.get("description") or ""):
            fields["description"] = entry.description
        if fields:
            snap.before_update_evidence(
                entry.gw.id,
                {
                    "caption": row.get("caption") or "",
                    "friendlyName": row.get("friendlyName") or "",
                    "description": row.get("description") or "",
                },
            )
            returned = client.update_evidence(entry.gw.id, fields)
            _emit(on_event, f"evidence meta ↑ {Path(entry.file).name}")
            if "friendlyName" in fields:
                result.warnings.append(
                    f"{_rel(root, path)}: {Path(entry.file).name} friendly_name changed — "
                    "Ghostwriter may asynchronously rewrite {{.Name}} references across this "
                    "report's findings; the next sync may pull updated prose"
                )
            # adopt the server's stored values, not the local strings just sent — same
            # rationale as the tag re-fetch (never assume a verbatim echo).
            entry.caption = returned.get("caption") or ""
            entry.friendly_name = returned.get("friendlyName") or ""
            entry.description = returned.get("description") or ""
            entry.gw.meta = _row_meta(returned)
        else:
            entry.gw.meta = local_meta
        finalize(path)
    elif outcome == "remote_ahead":
        entry.caption = row.get("caption") or ""
        entry.friendly_name = row.get("friendlyName") or ""
        entry.description = row.get("description") or ""
        entry.gw.meta = remote_meta
        finalize(path)
        _emit(on_event, f"evidence meta ↓ {Path(entry.file).name}")
    else:  # collision — preserve local's on-disk values, leave the base stale
        msg = _evidence_meta_collision_msg(_rel(root, path), entry.file)
        result.errors.append(msg)
        _emit(on_event, msg)


def _adopt_server_evidence_names(
    f: Finding,
    path: Path,
    client: GhostwriterClient,
    uploaded_ids: list[int],
    result: SyncResult,
    root: Path,
    finalize: Callable[[Path], None],
    *,
    on_event: Callable[[str], None] | None = None,
) -> None:
    """After a batch of uploads: Django's storage appends ``_<rand7>`` to the stored
    filename on a collision (F6) — adopt whatever Ghostwriter actually stored onto the
    local file/frontmatter so ``entry.file``/``gw.basename`` keep mirroring GW's
    storage path exactly (the local rename guard at classify time depends on this)."""
    rows_by_id = {r["id"]: r for r in client.fetch_evidence_by_ids(uploaded_ids)}
    for entry in f.evidence:
        if entry.gw is None or entry.gw.id not in rows_by_id:
            continue
        stored = evidence_basename(rows_by_id[entry.gw.id])
        sent = Path(entry.file).name
        if stored == sent:
            continue
        old_img = path.parent / entry.file
        new_rel = str(Path(entry.file).with_name(stored))
        new_img = path.parent / new_rel
        old_img.rename(new_img)
        entry.file = new_rel
        entry.gw.basename = stored
        result.warnings.append(
            f"{_rel(root, path)}: Ghostwriter renamed uploaded evidence {sent!r} to "
            f"{stored!r} (filename collision) — local file renamed to match"
        )
        _emit(on_event, f"evidence renamed ↺ {sent} → {stored}")
        finalize(path)


def _push_evidence(
    f: Finding,
    path: Path,
    client: GhostwriterClient,
    snap: Snapshot,
    result: SyncResult,
    remote_rows: list[dict] | None,
    root: Path,
    *,
    finalize: Callable[[Path], None],
    on_event: Callable[[str], None] | None = None,
) -> None:
    """Upload pending/stale local evidence, re-download an already-uploaded image whose
    local file vanished (F2 — never silently stays 'clean'), reconcile per-image
    caption/friendly_name/description drift (gw-push-2), adopt any server-renamed
    upload basename, then delete any remote row an image no longer claims (removed-
    from-frontmatter or superseded-by-a-reupload) — all snapshot-backed so a bad batch
    is fully reversible. ``finalize`` persists the push-side canonical shell (see
    :func:`_finalize_canonical`), not ``f`` itself, after every mutating step, so a
    crash mid-batch still leaves each id/basename durable on disk."""
    if f.grison.tier != "instance":
        return
    remote_by_id = {r["id"]: r for r in remote_rows or []}
    uploaded_ids: list[int] = []
    for entry in f.evidence:
        stale = _is_stale_evidence(entry, path.parent)
        missing = _is_missing_evidence(entry, path.parent)
        if entry.gw is not None and entry.gw.id is not None and not stale and not missing:
            # already uploaded, bytes on disk unchanged — but caption/friendly_name/
            # description may still have drifted on either side.
            row = remote_by_id.get(entry.gw.id)
            if row is not None:
                _reconcile_evidence_meta(
                    f, entry, row, path, client, snap, result, root,
                    finalize=finalize, on_event=on_event,
                )
            continue
        if entry.gw is not None and entry.gw.id is not None and missing:
            # already uploaded, but the local file is gone (deleted, moved out from
            # under grison) — re-download instead of erroring on a file that never
            # needed (re-)uploading, or silently leaving the record 'clean' forever (F2).
            row = remote_by_id.get(entry.gw.id)
            _name, data = client.download_evidence(entry.gw.id)
            img = path.parent / entry.file
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(data)
            entry.gw.hash = _image_hash(data)
            if row is not None:
                entry.gw.meta = _row_meta(row)
            result.evidence_down += 1
            _emit(on_event, f"evidence ↓ {Path(entry.file).name} (restored — missing locally)")
            finalize(path)
            continue
        img = path.parent / entry.file
        if not img.exists():
            # never uploaded (no gw.id) and the file is gone too — nothing to recover
            # from; unlike the missing-with-gw.id case above, this must surface loudly.
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
            description=entry.description,
            file_base64=base64.b64encode(data).decode(),
        )
        snap.after_upload_evidence(new_id)
        entry.gw = EvidenceGwRef(
            id=new_id, hash=_image_hash(data), meta=_entry_meta(entry), basename=filename,
        )  # old row (if any) reaped below
        result.evidence_up += 1
        uploaded_ids.append(new_id)
        _emit(on_event, f"evidence ↑ {filename}")
        finalize(path)  # persist this upload's id before attempting the next

    if uploaded_ids:
        _adopt_server_evidence_names(
            f, path, client, uploaded_ids, result, root, finalize, on_event=on_event
        )

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
            row.get("description") or "",
            base64.b64encode(data).decode(),
        )
        client.delete_evidence(row["id"])
        result.evidence_deleted += 1
        _emit(on_event, f"evidence ✕ {filename}")
