"""``validate_workspace`` — the ONE offline validator for workspace format v2.

Never raises on a malformed DOCUMENT: every content problem, however the input is
broken, becomes a :class:`~grison.validator.registry.Failure` in the returned list.
It DOES raise — :class:`~grison.validator.errors.ValidationScopeError` — for a
``paths=`` argument that doesn't resolve to a real, in-workspace, validated location;
see the ``_Scope`` section below for why silently matching nothing is a worse failure
mode than raising. Nothing here contacts a network or needs credentials.

This is a package, not a single module — each submodule owns one slice of the rule
set (see each module's own docstring); this file wires them together and is the
entry point everything else imports from.
"""

from __future__ import annotations

from pathlib import Path

from grison.validator.core.findings import (
    _check_findings_toplevel,
    _check_library_flat,
    _validate_finding_file,
    _validate_inbox_dir,
)
from grison.validator.core.index_rules import _check_index_shapes, _load_index
from grison.validator.core.layout import (
    _check_manifest_and_hygiene,
    _check_methodology_toplevel,
    _check_shelves,
    _load_terms,
)
from grison.validator.core.names import _check_evidence_extension as _check_evidence_extension
from grison.validator.core.names import _check_fileset_name as _check_fileset_name
from grison.validator.core.names import _check_names
from grison.validator.core.reports import _validate_report_dir
from grison.validator.core.scaffold_rules import _check_scaffolded_files
from grison.validator.core.scope import _narrowed, _resolve_given_path, _Scope, _scope_everything
from grison.validator.core.tree import _discover_library_files
from grison.validator.core.wiki import _validate_book_dir
from grison.validator.core.wiki_slugs import _bs_host, _checklist_slugs, _collect_wiki_slugs
from grison.validator.registry import Failure


def validate_workspace(
    root: Path, *, paths: list[Path] | None = None, deleted_ok: bool = False
) -> list[Failure]:
    """Validate ``root`` (a workspace) entirely, or (when ``paths`` is given) just the
    scope those paths name: a path equal to the workspace root means everything; a
    directory means everything under it plus the cross-file rules touching it
    (evidence/image stems+captions, mirrors, index consistency); a file means itself
    plus those same cross-file rules for its containing report/book/checklist
    directory. A path outside the workspace, one that doesn't exist, or one that isn't
    a validated location raises :class:`~grison.validator.errors.ValidationScopeError`
    — never silently treated as nothing to check (see the module comment above
    ``_Scope``). ``deleted_ok=True`` additionally accepts a path that no longer exists
    (e.g. the post-edit hook running after a delete) by falling back to its parent
    directory's scope.
    """
    root = root.resolve()
    out: list[Failure] = []
    out.extend(_check_manifest_and_hygiene(root))
    out.extend(_check_scaffolded_files(root))
    index, index_failures = _load_index(root)
    out.extend(index_failures)
    if index is not None:
        out.extend(_check_index_shapes(root, index))
    cterms = _load_terms(root)
    library_slugs = _collect_wiki_slugs(root / "methodology" / "library")
    bs_host = _bs_host(root)

    if paths is None:
        scope = _scope_everything(root)
    else:
        scope = _Scope()
        for given_path in paths:
            scope.merge(_resolve_given_path(root, given_path, deleted_ok=deleted_ok))

    if scope.check_findings_top:
        out.extend(_check_findings_toplevel(root))
    if scope.check_library_flat:
        out.extend(_check_library_flat(root))
    if scope.check_methodology_top:
        out.extend(_check_methodology_toplevel(root))
    if scope.check_shelves:
        out.extend(_check_shelves(root))

    if scope.library_all:
        for p in _discover_library_files(root):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "library", None, cterms))
    else:
        for p in sorted(scope.library_files):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "library", None, cterms))

    if scope.inbox_all:
        out.extend(_validate_inbox_dir(root, index, cterms))
    else:
        for p in sorted(scope.inbox_files):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "inbox", None, cterms))

    for d in sorted(scope.report_dirs):
        out.extend(_narrowed(scope, d, _validate_report_dir(root, d, index, cterms)))
    for d in sorted(scope.book_dirs):
        out.extend(
            _narrowed(scope, d, _validate_book_dir(root, d, index, cterms, library_slugs, bs_host))
        )
    for d in sorted(scope.checklist_dirs):
        cslugs = _checklist_slugs(root / d, library_slugs)
        out.extend(
            _narrowed(
                scope,
                d,
                _validate_book_dir(
                    root,
                    d,
                    None,
                    cterms,
                    cslugs,
                    bs_host,
                    check_mirror_digest=False,
                    noun="checklist",
                ),
            )
        )
    return out
