"""Directory/file discovery helpers over the workspace tree — no rules fire here,
these just enumerate what exists for the callers that do."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.validator.core.common import _rel


def _discover_report_dirs(root: Path) -> list[PurePosixPath]:
    base = root / "findings" / "reports"
    if not base.is_dir():
        return []
    return [PurePosixPath(_rel(root, d)) for d in sorted(base.iterdir()) if d.is_dir()]


def _discover_book_dirs(root: Path, rel_base: str) -> list[PurePosixPath]:
    """Every book-shaped directory directly under ``root / rel_base`` — real books
    under ``methodology/library``, or engagement copies under
    ``methodology/checklists`` (same shape, ``rel_base`` picks which)."""
    base = root / rel_base
    if not base.is_dir():
        return []
    return [
        PurePosixPath(_rel(root, d))
        for d in sorted(base.iterdir())
        if d.is_dir() and d.name != ".shelves"
    ]


def _discover_library_files(root: Path) -> list[PurePosixPath]:
    lib_dir = root / "findings" / "library"
    if not lib_dir.is_dir():
        return []
    return [PurePosixPath(_rel(root, p)) for p in sorted(lib_dir.glob("*.md"))]
