"""The tracked path -> remote identity index (brief D3 / "Workspace format v2").

Documents carry no machine identity fields (D3): a git-tracked, grison-owned index
file maps a workspace-relative path to a remote record's kind + id, so copying a
file creates a new record and a plain ``git mv`` is enough for git history to follow
a rename — the index just needs to be told the new path (:meth:`Index.move`).

The file lives at ``.grison/index.json`` (a *tracked* file inside an otherwise
untracked directory — see the workspace-format spec's ``.gitignore`` allow-list) and
is deliberately hand-formatted rather than a single ``json.dumps`` blob: keys are
sorted and every record sits on its own line, so two branches that each add or move
a different record produce a diff a plain three-way text merge (``git merge-file``,
no semantic JSON merge driver needed) can usually reconcile on its own.

This module is not wired into the sync engine yet — that's the later engine step.
It's a complete, independently-tested primitive: the engine will call
:meth:`Index.load`/:meth:`Index.set`/:meth:`Index.move`/:meth:`Index.remove` and
:meth:`Index.save` around its reconcile loop.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from grison.errors import GrisonError
from grison.fsio import atomic_write_text

INDEX_RELATIVE_PATH = ".grison/index.json"
_CURRENT_VERSION = 1


class IndexFileError(GrisonError, ValueError):
    """``.grison/index.json`` failed to load, or an operation on :class:`Index`
    would leave it in a shape that couldn't be loaded back — every message names the
    offending entry (path, kind, or id) so the fix is obvious."""


class IndexKind(StrEnum):
    """Every remote record kind an indexed path can point at."""

    GW_FINDING = "gw.finding"
    GW_REPORTED_FINDING = "gw.reportedFinding"
    GW_REPORT = "gw.report"
    GW_REPORT_SECTION = "gw.reportSection"
    GW_EVIDENCE = "gw.evidence"
    GW_PROJECT_NOTE = "gw.projectNote"
    BS_SHELF = "bs.shelf"
    BS_BOOK = "bs.book"
    BS_CHAPTER = "bs.chapter"
    BS_PAGE = "bs.page"
    BS_IMAGE = "bs.image"


@dataclass(frozen=True)
class IndexRecord:
    """One indexed identity: what kind of remote record, and its id."""

    kind: IndexKind
    id: int


def _validate_path(path: str) -> None:
    """POSIX-relative paths only — the index is meant to be diffed/merged as plain
    text across platforms, and an absolute path or a ``..`` segment would let an
    entry point outside the workspace entirely."""
    if not path:
        raise IndexFileError("index path must not be empty")
    if "\\" in path:
        raise IndexFileError(f"{path!r}: backslashes are not allowed (POSIX paths only)")
    p = PurePosixPath(path)
    if p.is_absolute():
        raise IndexFileError(f"{path!r}: absolute paths are not allowed")
    if ".." in p.parts:
        raise IndexFileError(f"{path!r}: '..' path segments are not allowed")


def _dumps(version: int, records: dict[str, IndexRecord]) -> str:
    """One record per line, paths sorted — see the module docstring."""
    if not records:
        body = "{}"
    else:
        lines = []
        for path in sorted(records):
            rec = records[path]
            entry = json.dumps({"id": rec.id, "kind": rec.kind.value}, sort_keys=True)
            lines.append(f"    {json.dumps(path)}: {entry}")
        body = "{\n" + ",\n".join(lines) + "\n  }"
    return f'{{\n  "version": {version},\n  "records": {body}\n}}\n'


@dataclass
class Index:
    """The loaded ``.grison/index.json`` for one workspace. All mutation is
    in-memory; call :meth:`save` to persist."""

    root: Path
    records: dict[str, IndexRecord] = field(default_factory=dict)
    version: int = _CURRENT_VERSION

    @staticmethod
    def path_for(root: Path) -> Path:
        return root / INDEX_RELATIVE_PATH

    @classmethod
    def load(cls, root: Path) -> Index:
        """Load ``.grison/index.json`` under ``root``. A missing file loads as an
        empty index (a fresh workspace, or one predating the index). Every other
        failure — malformed JSON, an entry with an unknown kind or non-int id, an
        entry with an unexpected field, a non-POSIX-relative path, or the same
        identity indexed under two different paths — raises :class:`IndexFileError`
        naming the offending entry; a strict load never silently drops or guesses."""
        path = cls.path_for(root)
        if not path.exists():
            return cls(root=root, records={})
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise IndexFileError(f"{path}: could not read index: {e}") from e
        except ValueError as e:
            raise IndexFileError(f"{path}: invalid JSON: {e}") from e
        if not isinstance(raw, dict) or "records" not in raw:
            raise IndexFileError(f"{path}: not a valid index (missing 'records')")
        version = raw.get("version", _CURRENT_VERSION)
        if not isinstance(version, int):
            raise IndexFileError(f"{path}: 'version' must be an integer")
        records_raw = raw.get("records")
        if not isinstance(records_raw, dict):
            raise IndexFileError(f"{path}: 'records' must be an object")

        records: dict[str, IndexRecord] = {}
        seen_identity: dict[tuple[str, int], str] = {}
        for rel_path, entry in records_raw.items():
            _validate_path(rel_path)
            if not isinstance(entry, dict):
                raise IndexFileError(f"{path}: entry {rel_path!r} is not an object")
            extra = set(entry) - {"kind", "id"}
            if extra:
                raise IndexFileError(
                    f"{path}: entry {rel_path!r} has unexpected field(s) {sorted(extra)}"
                )
            kind_raw = entry.get("kind")
            if not isinstance(kind_raw, str):
                raise IndexFileError(
                    f"{path}: entry {rel_path!r} has unknown kind {kind_raw!r}"
                )
            try:
                kind = IndexKind(kind_raw)
            except ValueError:
                raise IndexFileError(
                    f"{path}: entry {rel_path!r} has unknown kind {kind_raw!r}"
                ) from None
            ident = entry.get("id")
            if not isinstance(ident, int) or isinstance(ident, bool):
                raise IndexFileError(
                    f"{path}: entry {rel_path!r} has a non-integer id {ident!r}"
                )
            identity = (kind.value, ident)
            if identity in seen_identity:
                raise IndexFileError(
                    f"{path}: identity {kind.value} {ident} is indexed under two paths: "
                    f"{seen_identity[identity]!r} and {rel_path!r}"
                )
            seen_identity[identity] = rel_path
            records[rel_path] = IndexRecord(kind=kind, id=ident)
        return cls(root=root, records=records, version=version)

    def save(self) -> None:
        """Write the index back atomically. This file is TRACKED (part of the
        workspace-format spec's ``.gitignore`` allow-list inside ``.grison/``), so
        unlike the private state under ``.grison/state/`` it is never written
        ``private=True``."""
        atomic_write_text(self.path_for(self.root), _dumps(self.version, self.records))

    # --- lookups -----------------------------------------------------------

    def get(self, path: str) -> IndexRecord | None:
        _validate_path(path)
        return self.records.get(path)

    def path_of(self, kind: IndexKind, id: int) -> str | None:
        for p, rec in self.records.items():
            if rec.kind is kind and rec.id == id:
                return p
        return None

    def under(self, dir_path: str) -> dict[str, IndexRecord]:
        """Every record whose path sits below ``dir_path`` (a directory, not a
        record itself)."""
        _validate_path(dir_path)
        prefix = dir_path.rstrip("/") + "/"
        return {p: r for p, r in self.records.items() if p.startswith(prefix)}

    def missing_on_disk(self, root: Path) -> list[str]:
        """Indexed paths with no file at that location any more (sorted)."""
        return sorted(p for p in self.records if not (root / p).exists())

    def unindexed(self, root: Path, candidates: Iterable[str]) -> list[str]:
        """Of ``candidates`` (workspace-relative paths a caller found on disk),
        the ones not currently indexed (sorted)."""
        return sorted(set(candidates) - set(self.records))

    # --- mutation ------------------------------------------------------------

    def set(self, path: str, kind: IndexKind, id: int) -> None:
        """Index ``path`` as ``kind``/``id`` — a new record, or an idempotent
        re-set of the same path's existing identity. Refuses to create a second
        path for an identity already indexed elsewhere: that's what :meth:`move`
        is for, and allowing it silently here would reproduce the same
        two-paths-one-identity shape :meth:`load` refuses to read back."""
        _validate_path(path)
        existing = self.path_of(kind, id)
        if existing is not None and existing != path:
            raise IndexFileError(
                f"identity {kind.value} {id} is already indexed under {existing!r}; "
                f"use move({existing!r}, {path!r}) to reassign it"
            )
        self.records[path] = IndexRecord(kind=kind, id=id)

    def remove(self, path: str) -> None:
        """Drop ``path``'s entry, if any. Never an error to remove an unindexed
        path — callers don't need to check first."""
        _validate_path(path)
        self.records.pop(path, None)

    def move(self, old: str, new: str) -> None:
        """Reassign an identity from ``old`` to ``new`` (a rename/relocate)."""
        _validate_path(old)
        _validate_path(new)
        if old not in self.records:
            raise IndexFileError(f"{old!r} is not indexed")
        if new in self.records and new != old:
            raise IndexFileError(f"{new!r} is already indexed — remove it first")
        if new == old:
            return
        self.records[new] = self.records.pop(old)
