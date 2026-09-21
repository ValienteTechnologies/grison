"""``findings/reports/<dir>/`` validation: the report directory itself (instance
findings, narrative, notes, evidence), plus its ``.report.yml``/``project.md``
mirrors.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.engine.sidecar import is_sidecar_name
from grison.formats import mirrors as mirrors_fmt
from grison.formats import narrative as narrative_fmt
from grison.formats import note as note_fmt
from grison.formats.common import FormatError
from grison.index import Index, IndexKind
from grison.markdown.converter import ConverterError, md_to_html
from grison.validator import registry
from grison.validator.core.common import (
    _check_mirror,
    _check_txt,
    _EmbedHit,
    _read_text,
    _rel,
)
from grison.validator.core.findings import _validate_finding_file
from grison.validator.core.index_rules import _check_index_kind, _entry_kind
from grison.validator.core.names import _check_fileset_name, _check_names
from grison.validator.refs import OfflineEvidenceResolver, cross_refs, embeds, stems
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm


def _validate_report_dir(
    root: Path, report_dir: PurePosixPath, index: Index | None, cterms: list[ConfidentialTerm]
) -> list[Failure]:
    out: list[Failure] = []
    rel_dir = report_dir.as_posix()
    # WS-004 (retired): a report directory's name is a stable handle, not derived
    # data — a freshly-pulled one is slug(title) with no numeric prefix at all, and an
    # existing v1 "<id>-<slug>" name stays valid forever (D4). Only WS-001 (name
    # charset, via _check_names above) and IDX-003 (below — a document inside a
    # directory not indexed as gw.report) apply to a report directory's name/identity.
    out.extend(_check_names(report_dir))

    indexed = index is not None and _entry_kind(index, rel_dir) == IndexKind.GW_REPORT
    abs_dir = root / report_dir
    evidence_dir = abs_dir / "evidence"

    known_top = {"evidence", "narrative", "notes", ".report.yml", "project.md"}
    for p in sorted(abs_dir.iterdir()) if abs_dir.is_dir() else []:
        if p.name in known_top or (p.is_file() and p.name.endswith(".md")):
            continue
        out.append(
            fail(
                registry.WS_UNKNOWN_FINDINGS_PATH,
                _rel(root, p),
                "unexpected entry directly inside a report directory",
            )
        )
    for sub in ("narrative", "notes"):
        subdir = abs_dir / sub
        if subdir.is_dir():
            for p in sorted(subdir.iterdir()):
                if p.is_dir() or not p.name.endswith(".md"):
                    out.append(
                        fail(
                            registry.WS_UNKNOWN_FINDINGS_PATH,
                            _rel(root, p),
                            f"only *.md files are allowed directly in {sub}/",
                        )
                    )

    all_files = sorted(p for p in abs_dir.rglob("*") if p.is_file())
    if index is not None and not indexed:
        for p in all_files:
            out.append(fail(registry.IDX_UNINDEXED_REPORT_DIR, _rel(root, p), rel_dir))

    embed_hits: list[_EmbedHit] = []

    for md_path in sorted(abs_dir.glob("*.md")):
        if md_path.name == "project.md":  # a mirror (§3.3), not a finding — handled below
            continue
        rel = _rel(root, md_path)
        out.extend(_check_names(PurePosixPath(rel)))
        out.extend(_validate_finding_file(root, rel, "instance", evidence_dir, cterms))
        text = md_path.read_text(encoding="utf-8", errors="replace")
        for ref in embeds(text, prefix="evidence"):
            embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    narrative_order = _read_narrative_order(abs_dir / ".report.yml")
    narrative_dir = abs_dir / "narrative"
    if narrative_dir.is_dir():
        for md_path in sorted(narrative_dir.glob("*.md")):
            rel = _rel(root, md_path)
            out.extend(_check_names(PurePosixPath(rel)))
            if narrative_order is not None and md_path.stem not in narrative_order:
                out.append(
                    fail(
                        registry.REP_UNKNOWN_NARRATIVE_FIELD,
                        rel,
                        f"field {md_path.stem!r} is not in {sorted(narrative_order)}",
                    )
                )
            ntext, read_fail = _read_text(root, rel, default_rule=registry.REP_BODY_NOT_CONVERTIBLE)
            if ntext is None:
                out.extend([read_fail] if read_fail else [])
                continue
            out.extend(_check_txt(rel, ntext, cterms))
            doc = narrative_fmt.parse(ntext, path=md_path)
            out.extend(_check_narrative_body(rel, doc.body, evidence_dir))
            for ref in embeds(doc.body, prefix="evidence"):
                embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    notes_dir = abs_dir / "notes"
    if notes_dir.is_dir():
        for md_path in sorted(notes_dir.glob("*.md")):
            rel = _rel(root, md_path)
            out.extend(_check_names(PurePosixPath(rel)))
            out.extend(_validate_note_file(root, rel, index, cterms, evidence_dir))

    if evidence_dir.is_dir():
        # A live collision sidecar (`<name>.remote.<ext>`) is never a document/file
        # the engine manages (ENGINE.md §8) — excluded from REF-003's stem
        # comparison and from index-kind checking the same way
        # `grison.engine.filesets`'s own local scan and `grison status`'s evidence
        # counts already exclude it (item 5), or it could spuriously enter the
        # stem comparison or get treated as an unindexed evidence file. Its OWN
        # name is still checked below, by every file (sidecar-shaped or not) —
        # REF-008 rejects the sidecar shape outright, it is never silently
        # exempt from having SOME valid name.
        filenames = sorted(
            p.name for p in evidence_dir.iterdir() if p.is_file() and not is_sidecar_name(p.name)
        )
        for stem, group in stems(filenames).items():
            if len(group) > 1:
                for name in group:
                    out.append(
                        fail(
                            registry.REF_STEM_COLLISION,
                            _rel(root, evidence_dir / name),
                            f"shares stem {stem!r} with {[g for g in group if g != name]}",
                        )
                    )
        evidence_dir_rel = PurePosixPath(_rel(root, evidence_dir))
        for p in sorted(evidence_dir.iterdir()):
            if p.is_file():
                out.extend(_check_names(PurePosixPath(_rel(root, p)), skip_last=True))
                out.extend(_check_fileset_name(evidence_dir_rel, p.name))
                if not is_sidecar_name(p.name) and index is not None:
                    out.extend(_check_index_kind(root, p, index, IndexKind.GW_EVIDENCE))

    # REF-004: disagreeing non-empty captions for the same referenced file
    by_path: dict[str, list[_EmbedHit]] = {}
    for hit in embed_hits:
        by_path.setdefault(hit.path, []).append(hit)
    for path, hits in by_path.items():
        captions = {h.caption for h in hits if h.caption.strip()}
        if len(captions) > 1:
            for h in hits:
                out.append(
                    fail(
                        registry.REF_CAPTION_CONFLICT,
                        h.doc_rel,
                        f"{path!r} captioned {sorted(captions)} across this report",
                        line=h.line,
                    )
                )

    for name in (".report.yml", "project.md"):
        p = abs_dir / name
        if p.is_file():
            rel = _rel(root, p)
            text = p.read_text(encoding="utf-8", errors="replace")
            out.extend(_check_mirror(root, rel, text))
            try:
                if name == ".report.yml":
                    mirrors_fmt.parse_report_meta(text, path=p)
                else:
                    mirrors_fmt.parse_project_context(text, path=p)
            except FormatError as e:
                out.append(fail(registry.WS_MIRROR_MALFORMED, rel, e.detail or e.kind))

    return out


def _read_narrative_order(meta_path: Path) -> set[str] | None:
    """``.report.yml``'s recorded ``narrative_order`` (REP-003's only offline source
    of truth for "which fields are real" — the validator never contacts Ghostwriter).
    ``None`` when there is nothing to compare against yet: no ``.report.yml`` (never
    synced), a malformed one (``WS-010`` already reports that separately), or one
    with an empty ``narrative_order`` — matches the "nothing recorded yet" convention
    ``WS-009``'s digest check uses."""
    if not meta_path.is_file():
        return None
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
        doc = mirrors_fmt.parse_report_meta(text, path=meta_path)
    except FormatError:
        return None
    return set(doc.narrative_order) or None


def _check_narrative_body(rel: str, text: str, evidence_dir: Path) -> list[Failure]:
    if not text.strip():
        return []
    out: list[Failure] = []
    ref_issue = False
    for ref in embeds(text, prefix="evidence"):
        if not ref.standalone:
            out.append(
                fail(
                    registry.REF_BAD_POSITION,
                    rel,
                    f"{ref.path!r} is not alone in its block",
                    line=ref.line,
                )
            )
            ref_issue = True
        elif not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(
                fail(registry.REF_UNRESOLVED, rel, f"{ref.path!r} does not resolve", line=ref.line)
            )
            ref_issue = True
    for ref in cross_refs(text, prefix="evidence"):
        if not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(
                fail(
                    registry.REF_BAD_CROSS_REFERENCE,
                    rel,
                    f"{ref.path!r} does not resolve",
                    line=ref.line,
                )
            )
            ref_issue = True
    if not ref_issue:
        try:
            md_to_html(text, headings=True, refs=OfflineEvidenceResolver(evidence_dir))
        except ConverterError as e:
            out.append(fail(registry.REP_BODY_NOT_CONVERTIBLE, rel, str(e)))
    return out


def _validate_note_file(
    root: Path,
    rel: str,
    index: Index | None,
    cterms: list[ConfidentialTerm],
    evidence_dir: Path,
) -> list[Failure]:
    out: list[Failure] = []
    text, read_fail = _read_text(root, rel, default_rule=registry.REP_BAD_NOTE)
    if text is None:
        return [read_fail] if read_fail else []
    out.extend(_check_txt(rel, text, cterms))
    try:
        doc = note_fmt.parse(text, path=Path(rel))
    except FormatError as e:
        out.append(fail(registry.REP_BAD_NOTE, rel, e.detail or e.kind, line=e.line))
        return out

    is_indexed = index is not None and _entry_kind(index, rel) == IndexKind.GW_PROJECT_NOTE
    if index is not None:
        if is_indexed and not doc.has_frontmatter:
            out.append(
                fail(
                    registry.REP_BAD_NOTE,
                    rel,
                    "indexed note has no frontmatter (expected a mirrored note)",
                )
            )
        elif not is_indexed and doc.has_frontmatter:
            out.append(
                fail(
                    registry.REP_BAD_NOTE,
                    rel,
                    "unindexed note has frontmatter (a new local note must not)",
                )
            )

    out.extend(_check_narrative_body(rel, doc.body, evidence_dir))
    return out
