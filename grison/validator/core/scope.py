"""Scope resolution (§9 of the spec).

A `paths=` argument names a SCOPE, never a filter that can silently match nothing:
every given path is resolved to real validation units (below) or the call raises
ValidationScopeError — there is no way to spell a path that makes validate_workspace
return an empty, "clean-looking" result for something that wasn't actually checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from grison.validator.core.tree import _discover_book_dirs, _discover_report_dirs
from grison.validator.errors import ValidationScopeError
from grison.validator.registry import Failure


@dataclass
class _Scope:
    library_files: set[PurePosixPath] = field(default_factory=set)
    library_all: bool = False
    inbox_files: set[PurePosixPath] = field(default_factory=set)
    inbox_all: bool = False
    report_dirs: set[PurePosixPath] = field(default_factory=set)
    book_dirs: set[PurePosixPath] = field(default_factory=set)
    checklist_dirs: set[PurePosixPath] = field(default_factory=set)
    check_findings_top: bool = False
    check_library_flat: bool = False
    check_methodology_top: bool = False
    check_shelves: bool = False
    #: directory -> the exact file(s) within it to narrow this directory's own
    #: failures down to (task item 7): a single FILE argument inside a report/book/
    #: checklist directory reports only that file's own failures plus cross-file
    #: failures that involve it — every such rule (REF-003/004, IDX-003, WS-009/010,
    #: WIKI-007, …) already files its failure under the SPECIFIC document it
    #: concerns (never one shared failure for a whole directory), so "narrow to file
    #: X" is exactly "keep only failures whose own path is X". Absent from this
    #: dict, or mapped to ``None``, means unnarrowed (today's whole-directory
    #: behaviour) — a DIRECTORY argument (or "the workspace root"/"findings"/
    #: "methodology") never narrows.
    narrow: dict[PurePosixPath, frozenset[PurePosixPath] | None] = field(default_factory=dict)

    def merge(self, other: _Scope) -> None:
        self.library_files |= other.library_files
        self.library_all = self.library_all or other.library_all
        self.inbox_files |= other.inbox_files
        self.inbox_all = self.inbox_all or other.inbox_all
        self.report_dirs |= other.report_dirs
        self.book_dirs |= other.book_dirs
        self.checklist_dirs |= other.checklist_dirs
        self.check_findings_top = self.check_findings_top or other.check_findings_top
        self.check_library_flat = self.check_library_flat or other.check_library_flat
        self.check_methodology_top = self.check_methodology_top or other.check_methodology_top
        self.check_shelves = self.check_shelves or other.check_shelves
        for d, files in other.narrow.items():
            existing = self.narrow.get(d)
            if d not in self.narrow:
                self.narrow[d] = files
            elif existing is None or files is None:
                self.narrow[d] = None  # either request wants the whole directory
            else:
                self.narrow[d] = existing | files  # union: both files' own failures


def _narrowed(scope: _Scope, d: PurePosixPath, failures: list[Failure]) -> list[Failure]:
    """Apply ``scope.narrow[d]`` (task item 7) to one directory's own failure list.
    Every rule this validator fires already files its failure under the specific
    document it concerns — including the cross-file ones (REF-003/004): each
    involved document gets its OWN failure entry with its own path, never one shared
    failure for a whole directory — so "narrow to file X" is exactly "keep only
    failures whose own path is X", with no extra cross-referencing logic needed."""
    only = scope.narrow.get(d)
    if only is None:
        return failures
    wanted = {f.as_posix() for f in only}
    return [f for f in failures if f.path in wanted]


def _scope_everything(root: Path) -> _Scope:
    return _Scope(
        library_all=True,
        inbox_all=True,
        report_dirs=set(_discover_report_dirs(root)),
        book_dirs=set(_discover_book_dirs(root, "methodology/library")),
        checklist_dirs=set(_discover_book_dirs(root, "methodology/checklists")),
        check_findings_top=True,
        check_library_flat=True,
        check_methodology_top=True,
        check_shelves=True,
    )


def _scope_for_path(root: Path, parts: tuple[str, ...], *, is_dir: bool) -> _Scope:
    """``parts`` is a non-empty, workspace-relative path (the root case is handled by
    the caller). Raises :class:`ValidationScopeError` for anything that isn't findings/
    or methodology/ content — a directory outside those trees, or a path this document
    doesn't otherwise recognize, is never silently treated as "nothing to check"."""
    s = _Scope()

    if parts[0] not in ("findings", "methodology"):
        raise ValidationScopeError(
            f"{'/'.join(parts)} is not a workspace document (only findings/ and "
            "methodology/ are validated)"
        )

    if parts == ("findings",):
        s.library_all = True
        s.inbox_all = True
        s.report_dirs = set(_discover_report_dirs(root))
        s.check_findings_top = True
        s.check_library_flat = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "library":
        # a real *.md file gets narrowed to just itself; a bare "findings/library",
        # a stray subdirectory, or a non-.md entry falls back to the whole area so
        # WS-002 ("only *.md files allowed here") still fires rather than being
        # silently skipped for the one path that would have caught it.
        if not is_dir and len(parts) == 3 and parts[2].endswith(".md"):
            s.library_files = {PurePosixPath(*parts)}
        else:
            s.library_all = True
        s.check_library_flat = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "inbox":
        if not is_dir and len(parts) == 3 and parts[2].endswith(".md"):
            s.inbox_files = {PurePosixPath(*parts)}
        else:
            s.inbox_all = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "reports":
        if len(parts) == 2:
            s.report_dirs = set(_discover_report_dirs(root))
        else:
            d = PurePosixPath(*parts[:3])
            s.report_dirs = {d}
            if len(parts) > 3 and not is_dir:
                # a FILE inside the report dir (task item 7): narrow to just its own
                # failures + whichever cross-file failures name it — never the whole
                # report's sibling files. A DIRECTORY argument (the report dir
                # itself, or narrative/notes/evidence as a subdirectory) is left
                # unnarrowed — "a DIRECTORY argument keeps today's behaviour".
                s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    if parts[0] == "findings":
        raise ValidationScopeError(f"{'/'.join(parts)} is not a validated workspace location")

    # methodology/…
    if parts == ("methodology",):
        s.book_dirs = set(_discover_book_dirs(root, "methodology/library"))
        s.checklist_dirs = set(_discover_book_dirs(root, "methodology/checklists"))
        s.check_methodology_top = True
        s.check_shelves = True
        return s

    if len(parts) >= 2 and parts[1] == "library":
        if len(parts) >= 3 and parts[2] == ".shelves":
            s.check_shelves = True
            return s
        if len(parts) == 2:
            s.book_dirs = set(_discover_book_dirs(root, "methodology/library"))
            s.check_shelves = True
            return s
        d = PurePosixPath(*parts[:3])
        s.book_dirs = {d}
        if len(parts) > 3 and not is_dir:
            s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    if len(parts) >= 2 and parts[1] == "checklists":
        if len(parts) == 2:
            s.checklist_dirs = set(_discover_book_dirs(root, "methodology/checklists"))
            return s
        d = PurePosixPath(*parts[:3])
        s.checklist_dirs = {d}
        if len(parts) > 3 and not is_dir:
            s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    raise ValidationScopeError(f"{'/'.join(parts)} is not a validated workspace location")


def _resolve_given_path(root: Path, given: Path, *, deleted_ok: bool) -> _Scope:
    ap = (given if given.is_absolute() else (Path.cwd() / given)).resolve()
    try:
        rel = ap.relative_to(root)
    except ValueError:
        raise ValidationScopeError(f"path is outside the workspace: {given}") from None
    rel_posix = PurePosixPath(rel.as_posix()) if str(rel) != "." else PurePosixPath()

    if not rel_posix.parts:  # the workspace root itself — "." / "./" / the root path
        return _scope_everything(root)

    exists = (root / rel_posix).exists()
    if not exists:
        if deleted_ok:
            parent = rel_posix.parent
            parent_abs = root / parent if parent.parts else root
            if not parent_abs.is_dir():
                raise ValidationScopeError(f"no such path: {given}")
            if not parent.parts:
                return _scope_everything(root)
            return _scope_for_path(root, parent.parts, is_dir=True)
        raise ValidationScopeError(f"no such path: {given}")

    if rel_posix.parts[0] == ".grison":
        raise ValidationScopeError(
            f"{rel_posix} is inside .grison/ (private, not a workspace document)"
        )

    return _scope_for_path(root, rel_posix.parts, is_dir=(root / rel_posix).is_dir())
