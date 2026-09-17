"""``.grison/manifest.yml`` — the workspace's format version (brief D13).

Older grison refuses to sync against a newer-format workspace and says to upgrade;
grison encountering an older-format workspace raises a distinct error the (later)
one-time migration step catches and handles, rather than trying to read the old
shape directly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from grison.errors import GrisonError
from grison.fsio import atomic_write_text

CURRENT_FORMAT = 2
MANIFEST_RELATIVE_PATH = ".grison/manifest.yml"

# .gitignore allow-list for .grison/ (brief's "Workspace format v2" layout table):
# ignore everything in the directory except these tracked files.
_GITIGNORE_TEXT = """\
# grison-managed — ignore everything in this directory except the files below.
*
!.gitignore
!manifest.yml
!index.json
!SPEC.md
!templates/
!templates/**
"""


class ManifestError(GrisonError, ValueError):
    """The manifest itself is unreadable/malformed — distinct from the two format-
    mismatch errors below, which mean the manifest read fine but disagrees with
    this grison's supported version."""


class WorkspaceTooNew(GrisonError, ValueError):
    """This workspace's format is newer than this grison supports — upgrade
    grison, not the workspace."""


class WorkspaceNeedsMigration(GrisonError, ValueError):
    """This workspace's format is older than this grison's current format — the
    one-time migration step (not this module) converts it."""


@dataclass(frozen=True)
class Manifest:
    format: int


def _path(root: Path) -> Path:
    return root / MANIFEST_RELATIVE_PATH


def read(root: Path) -> Manifest:
    """Read ``.grison/manifest.yml``. A workspace with v1 artefacts
    (``.grison/env`` exists) but no manifest reads as format 1 — the manifest
    itself postdates v1, so its absence in an otherwise-real workspace is v1, not
    "no workspace here" (that's a fresh, unbootstrapped directory instead, which
    callers handle before ever calling this)."""
    path = _path(root)
    if not path.exists():
        if (root / ".grison" / "env").exists():
            return Manifest(format=1)
        return Manifest(format=CURRENT_FORMAT)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ManifestError(f"{path}: could not read manifest: {e}") from e
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict) or "format" not in raw:
        raise ManifestError(f"{path}: missing 'format' key")
    fmt = raw["format"]
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise ManifestError(f"{path}: 'format' must be an integer, got {fmt!r}")
    return Manifest(format=fmt)


def write(root: Path, manifest: Manifest | None = None) -> None:
    """Write ``.grison/manifest.yml`` — tracked, not private (see
    :mod:`grison.index`'s save() for the same distinction)."""
    manifest = manifest or Manifest(format=CURRENT_FORMAT)
    text = yaml.safe_dump({"format": manifest.format}, sort_keys=False)
    atomic_write_text(_path(root), text)


def check(root: Path) -> Manifest:
    """Read the manifest and enforce it against ``CURRENT_FORMAT``: raises
    :class:`WorkspaceTooNew` when the workspace is newer than this grison
    supports, :class:`WorkspaceNeedsMigration` when it's older, and otherwise
    returns the (matching) manifest."""
    manifest = read(root)
    if manifest.format > CURRENT_FORMAT:
        raise WorkspaceTooNew(
            f"this workspace uses format {manifest.format}; this grison supports "
            f"up to {CURRENT_FORMAT} — upgrade grison"
        )
    if manifest.format < CURRENT_FORMAT:
        raise WorkspaceNeedsMigration(
            f"workspace format {manifest.format} needs the one-time migration"
        )
    return manifest


def write_gitignore(root: Path) -> None:
    """Write ``.grison/.gitignore`` as the allow-list from the brief: ignore
    everything in the directory except ``.gitignore``, ``manifest.yml``,
    ``index.json``, ``SPEC.md``, and ``templates/`` — fail-safe, so a new private
    file grison ever creates under ``.grison/`` is ignored by default unless this
    list is explicitly updated to un-ignore it."""
    atomic_write_text(root / ".grison" / ".gitignore", _GITIGNORE_TEXT)


def check_git_hygiene(root: Path) -> list[str]:
    """When ``root`` is inside a git repository, verify with ``git check-ignore``
    (read-only) that every private ``.grison/`` path is ignored and every tracked
    one is not. Returns a list of problem descriptions (empty when everything is
    correct, or when ``root`` isn't in a git repo at all — nothing to check)."""

    def _is_repo() -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    if not _is_repo():
        return []

    def _ignored(rel: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", rel],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    problems: list[str] = []
    must_be_ignored = [".grison/env", ".grison/state", ".grison/snapshots", ".grison/terms.txt"]
    must_be_tracked = [".grison/manifest.yml", ".grison/index.json"]
    for rel in must_be_ignored:
        if (root / rel).exists() and not _ignored(rel):
            problems.append(f"{rel} must be git-ignored but is not (private data would leak)")
    for rel in must_be_tracked:
        if (root / rel).exists() and _ignored(rel):
            problems.append(f"{rel} is git-ignored but must be tracked (would be silently lost)")
    return problems
