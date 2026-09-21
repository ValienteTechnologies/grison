"""Workspace-wide layout checks: the manifest/git-hygiene gate, the
``methodology/`` toplevel shape, ``.shelves/`` (WS-003/WS-010), and confidential-term
loading."""

from __future__ import annotations

from pathlib import Path

from grison import manifest as manifest_mod
from grison.formats import mirrors as mirrors_fmt
from grison.formats.common import FormatError
from grison.validator import registry
from grison.validator import terms as terms_mod
from grison.validator.core.common import _check_mirror, _rel
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm


def _check_manifest_and_hygiene(root: Path) -> list[Failure]:
    out: list[Failure] = []
    # Only check the format when there is an actual format to check (item 10,
    # fix-fin1) — grison.manifest.is_bootstrapped, the SAME precise signal
    # bootstrap_workspace itself uses. Without this, manifest.read()'s own
    # cruder fallback heuristic ("no manifest.yml, but .grison/env exists ->
    # format 1") would misreport a directory whose .grison/env merely exists
    # with no real content yet as "needs migration", contradicting
    # bootstrap_workspace's own documented decision to start such a directory
    # fresh at CURRENT_FORMAT.
    if manifest_mod.is_bootstrapped(root):
        try:
            manifest_mod.check(root)
        except manifest_mod.WorkspaceNeedsMigration as e:
            out.append(fail(registry.WS_NEEDS_MIGRATION, ".grison/manifest.yml", str(e)))
        except manifest_mod.WorkspaceTooNew as e:
            out.append(fail(registry.WS_TOO_NEW, ".grison/manifest.yml", str(e)))
        except manifest_mod.ManifestError as e:
            out.append(fail(registry.WS_BAD_MANIFEST, ".grison/manifest.yml", str(e)))

    for problem in manifest_mod.check_git_hygiene(root):
        out.append(fail(registry.WS_GIT_HYGIENE, ".grison", problem))
    return out


def _check_methodology_toplevel(root: Path) -> list[Failure]:
    out: list[Failure] = []
    methodology = root / "methodology"
    if not methodology.is_dir():
        return out
    known = {"library", "checklists"}
    for p in sorted(methodology.iterdir()):
        if p.name not in known:
            out.append(fail(registry.WS_UNKNOWN_METHODOLOGY_PATH, _rel(root, p), p.name))
    return out


def _check_shelves(root: Path) -> list[Failure]:
    """WS-003 (only ``*.yml`` files directly in ``.shelves/``) AND each shelf file's
    own schema (``WS-010`` — previously never actually wired up anywhere)."""
    out: list[Failure] = []
    shelves_dir = root / "methodology" / "library" / ".shelves"
    if not shelves_dir.is_dir():
        return out
    for p in sorted(shelves_dir.iterdir()):
        if p.is_dir() or not p.name.endswith(".yml"):
            out.append(
                fail(
                    registry.WS_UNKNOWN_METHODOLOGY_PATH,
                    _rel(root, p),
                    "only *.yml files are allowed directly in .shelves/",
                )
            )
            continue
        rel = _rel(root, p)
        text = p.read_text(encoding="utf-8", errors="replace")
        out.extend(_check_mirror(root, rel, text))
        try:
            mirrors_fmt.parse_shelf_mirror(text, path=p)
        except FormatError as e:
            out.append(fail(registry.WS_MIRROR_MALFORMED, rel, e.detail or e.kind))
    return out


def _load_terms(root: Path) -> list[ConfidentialTerm]:
    return terms_mod.load_terms(root)
