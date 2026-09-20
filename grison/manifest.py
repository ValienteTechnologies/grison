"""``.grison/manifest.yml`` — the workspace's format version (brief D13).

A workspace whose manifest format differs from ``CURRENT_FORMAT`` is refused
outright, with a plain message — nothing converts it. Older grison encountering a
newer-format workspace says to upgrade grison; this grison encountering an
older-format workspace raises a distinct error rather than trying to read the old
shape directly (there is no migration step — an older-format workspace stays on
the grison version that wrote it).
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

# Every ``.grison/`` entry that is explicitly TRACKED (brief's "Workspace format v2"
# layout table) — the ONE source both the ``.gitignore`` allow-list below and
# :mod:`grison.scaffold.settings_json`'s ``Read`` deny rules are derived from, so
# "what's safe to read" and "what's tracked" can never drift apart: an entry not in
# this tuple is private by construction (git-ignored, and denied to an agent's Read
# tool) until someone deliberately adds it here.
TRACKED_ENTRIES: tuple[str, ...] = (
    ".gitignore",
    "manifest.yml",
    "index.json",
    "SPEC.md",
    "templates/",
)

# Every KNOWN private ``.grison/`` entry — must stay git-ignored (see
# :func:`check_git_hygiene`) and is what :mod:`grison.scaffold.settings_json` denies
# an agent's Read tool for. Not derived from TRACKED_ENTRIES (there's no way to
# enumerate "everything else" as a static list) — this is the other, explicit half of
# the same allow-list design ``.grison/.gitignore``'s own ``*`` catch-all encodes.
PRIVATE_ENTRIES: tuple[str, ...] = ("env", "state/", "snapshots/", "lock", "terms.txt")

# .gitignore allow-list for .grison/: ignore everything in the directory except the
# tracked files above.
_GITIGNORE_TEXT = (
    "# grison-managed — ignore everything in this directory except the files below.\n"
    "*\n" + "".join(f"!{entry}\n" for entry in TRACKED_ENTRIES) + "!templates/**\n"
)


class ManifestError(GrisonError, ValueError):
    """The manifest itself is unreadable/malformed — distinct from the two format-
    mismatch errors below, which mean the manifest read fine but disagrees with
    this grison's supported version."""


class WorkspaceTooNew(GrisonError, ValueError):
    """This workspace's format is newer than this grison supports — upgrade
    grison, not the workspace."""


class WorkspaceNeedsMigration(GrisonError, ValueError):
    """This workspace's format is older than this grison's current format.
    Refused outright — there is no migration step that converts it; the workspace
    must be synced with the grison version that wrote it."""


@dataclass(frozen=True)
class Manifest:
    format: int


def _path(root: Path) -> Path:
    return root / MANIFEST_RELATIVE_PATH


def has_v1_content(root: Path) -> bool:
    """Whether ``root`` has any real pre-v2 content under ``findings/`` or
    ``methodology/`` — the precise signal that this is a genuine v1 workspace, as
    opposed to a directory whose ``.grison/env`` merely exists (by hand, copied
    in, or freshly template-written) with no real content yet, which
    :func:`grison.remote.bootstrap.bootstrap_workspace` deliberately starts at
    ``CURRENT_FORMAT`` instead — see its own docstring and :func:`read`'s "a
    workspace with v1 artefacts... but no manifest reads as format 1" comment,
    which this function's result feeds into everywhere that heuristic matters
    (item 10, fix-fin1: shared so the bootstrap-vs-refuse decision can never
    disagree with itself)."""
    return any(
        d.is_dir() and any(p.is_file() for p in d.rglob("*"))
        for d in (root / "findings", root / "methodology")
        if d.is_dir()
    )


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
    supports, :class:`WorkspaceNeedsMigration` when it's older (refused outright —
    D13, no conversion), and otherwise returns the (matching) manifest."""
    manifest = read(root)
    if manifest.format > CURRENT_FORMAT:
        raise WorkspaceTooNew(
            f"this workspace uses format {manifest.format}; this grison supports "
            f"up to {CURRENT_FORMAT} — upgrade grison"
        )
    if manifest.format < CURRENT_FORMAT:
        raise WorkspaceNeedsMigration(
            f"this workspace uses format {manifest.format}; this grison supports only "
            f"format {CURRENT_FORMAT} — no migration converts it, sync with the grison "
            f"version that wrote it"
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
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    if not _is_repo():
        return []

    def _ignored(rel: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", rel],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    problems: list[str] = []
    must_be_ignored = [f".grison/{entry.rstrip('/')}" for entry in PRIVATE_ENTRIES]
    must_be_tracked = [
        f".grison/{entry.rstrip('/')}" for entry in TRACKED_ENTRIES if entry != ".gitignore"
    ]
    for rel in must_be_ignored:
        if (root / rel).exists() and not _ignored(rel):
            problems.append(f"{rel} must be git-ignored but is not (private data would leak)")
    for rel in must_be_tracked:
        if (root / rel).exists() and _ignored(rel):
            problems.append(f"{rel} is git-ignored but must be tracked (would be silently lost)")
    return problems
