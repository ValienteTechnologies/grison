"""IDX-… rules: cross-checking ``.grison/index.json`` against what the workspace
tree's own shape implies, plus loading the index itself.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.index import Index, IndexFileError, IndexKind
from grison.validator import registry
from grison.validator.core.common import _rel
from grison.validator.registry import Failure, fail


def _entry_kind(index: Index, rel: str) -> IndexKind | None:
    rec = index.get(rel)
    return rec.kind if rec else None


def _check_index_kind(root: Path, path: Path, index: Index, expected: IndexKind) -> list[Failure]:
    rel = _rel(root, path)
    rec = index.get(rel)
    if rec is None:
        return []
    if rec.kind is expected:
        return []
    rule_id = registry.IDX_EVIDENCE_KIND_MISMATCH
    return [fail(rule_id, rel, f"indexed as {rec.kind.value}, expected {expected.value}")]


def _is_local_only(rel: PurePosixPath) -> bool:
    """``findings/inbox/`` and ``methodology/checklists/`` — validated (see
    ``_validate_inbox_dir``/``_validate_book_dir``), but NEVER synced and therefore
    NEVER a legitimate index entry."""
    parts = rel.parts
    return (len(parts) >= 2 and parts[0] == "findings" and parts[1] == "inbox") or (
        len(parts) >= 2 and parts[0] == "methodology" and parts[1] == "checklists"
    )


def _check_index_shapes(root: Path, index: Index) -> list[Failure]:
    """IDX-002/IDX-004 for every indexed path: does the recorded kind match what the
    path's own shape implies? A path under a local-only tree is a mismatch by
    construction — it can never correspond to any real remote kind."""
    out: list[Failure] = []
    for path, rec in sorted(index.records.items()):
        p = PurePosixPath(path)
        if _is_local_only(p):
            out.append(
                fail(
                    registry.IDX_KIND_PATH_MISMATCH,
                    path,
                    f"indexed as {rec.kind.value}, but this path is under a "
                    "local-only tree (findings/inbox/ or methodology/checklists/) "
                    "and must never be indexed",
                )
            )
            continue
        expected = _expected_kind(p)
        if expected is None:
            continue
        if rec.kind is not expected:
            rule_id = (
                registry.IDX_EVIDENCE_KIND_MISMATCH
                if "evidence" in p.parts or "images" in p.parts
                else registry.IDX_KIND_PATH_MISMATCH
            )
            out.append(
                fail(
                    rule_id,
                    path,
                    f"indexed as {rec.kind.value}, path shape implies {expected.value}",
                )
            )
    return out


def _expected_kind(rel: PurePosixPath) -> IndexKind | None:
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "findings" and parts[1] == "library":
        if len(parts) == 3 and parts[2].endswith(".md"):
            return IndexKind.GW_FINDING
        return None
    if len(parts) >= 3 and parts[0] == "findings" and parts[1] == "reports":
        if len(parts) == 3:
            return IndexKind.GW_REPORT
        if len(parts) == 4 and parts[3].endswith(".md"):
            return IndexKind.GW_REPORTED_FINDING
        if len(parts) == 5 and parts[3] == "evidence":
            return IndexKind.GW_EVIDENCE
        if len(parts) == 5 and parts[3] == "notes" and parts[4].endswith(".md"):
            return IndexKind.GW_PROJECT_NOTE
        if len(parts) == 5 and parts[3] == "narrative" and parts[4].endswith(".md"):
            return IndexKind.GW_REPORT_SECTION
        return None
    if len(parts) >= 3 and parts[0] == "methodology" and parts[1] == "library":
        if parts[2] == ".shelves":
            return IndexKind.BS_SHELF
        if len(parts) == 3:
            return IndexKind.BS_BOOK
        if len(parts) == 4 and parts[3].endswith(".md"):
            return IndexKind.BS_PAGE
        if len(parts) == 4:
            return IndexKind.BS_CHAPTER
        if len(parts) == 5 and parts[3] == "images":
            return IndexKind.BS_IMAGE
        if len(parts) == 5 and parts[4].endswith(".md"):
            return IndexKind.BS_PAGE
        return None
    return None


def _load_index(root: Path) -> tuple[Index | None, list[Failure]]:
    try:
        return Index.load(root), []
    except IndexFileError as e:
        return None, [fail(registry.IDX_BAD_INDEX, ".grison/index.json", str(e))]
