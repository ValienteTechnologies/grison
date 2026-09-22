"""WS-001 (path segment charset), REF-008 (a fileset entry's own bare name), and
REF-009 (a report evidence/ file's own extension)."""

from __future__ import annotations

from pathlib import PurePosixPath

from grison.engine.sidecar import is_sidecar_name
from grison.formats.common import NAME_RE
from grison.remote.ghostwriter.limits import (
    EVIDENCE_ALLOWED_EXTENSIONS,
    EVIDENCE_EXTENSION_SUGGESTIONS,
    evidence_extension,
    is_allowed_evidence_extension,
)
from grison.validator import registry
from grison.validator.registry import Failure, fail


def _is_valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def _check_names(rel: PurePosixPath, *, skip_last: bool = False) -> list[Failure]:
    """WS-001 against every path segment of ``rel`` (dotfiles like ``.report.yml``
    are exempt — grison itself names its own mirrors with a leading dot).
    ``skip_last=True`` (a file directly inside ``evidence/`` or ``images/`` —
    item 2, fix-findings: WS-001's charset rule does not apply to that one leaf
    name at all, see :func:`_check_fileset_name`/``REF-008``) checks every
    segment ABOVE the last one as normal — only the file's own bare name is
    exempt."""
    out: list[Failure] = []
    parts = rel.parts
    last = len(parts) - 1
    for i, part in enumerate(parts):
        if skip_last and i == last:
            continue
        if part.startswith("."):
            continue
        if not _is_valid_name(part):
            out.append(fail(registry.WS_BAD_NAME, rel.as_posix(), f"segment {part!r}"))
    return out


_FILESET_NAME_MAX_BYTES = 255


def _check_fileset_name(folder: PurePosixPath, name: str) -> list[Failure]:
    """``REF-008`` — the rule that applies to a file's OWN bare name directly
    inside ``evidence/`` or ``images/`` (``folder``) INSTEAD of ``WS-001``
    (item 2, fix-findings: these names are stable handles kept verbatim — D1/D9,
    real data has names like ``Phishing_Sonuçları.png``). Never duplicates
    ``REF-003`` (the cross-file stem-collision check, done separately over the whole
    folder) — this is purely a per-file shape check, and returns at most one
    failure (the first one that applies) rather than piling on redundant
    messages for one bad name.

    ``name`` is taken as a RAW STRING, never pre-split into a
    :class:`~pathlib.PurePosixPath` first: the two real callers (a directory
    scan) can only ever hand this a single filesystem entry's own name, which
    can never contain a "/" — but "no path separator" is still checked
    directly against ``name`` itself (a caller that DID have a compound
    candidate to check, e.g. a markdown reference's own destination fragment,
    would otherwise silently lose the separator the moment it got parsed into
    a path object, since ``PurePosixPath(...).name`` only ever returns the
    LAST component)."""
    full = f"{folder.as_posix()}/{name}"
    if "/" in name or "\\" in name:
        return [fail(registry.REF_BAD_FILESET_NAME, full, f"{name!r} contains a path separator")]
    if name.startswith("."):
        return [fail(registry.REF_BAD_FILESET_NAME, full, f"{name!r} starts with a leading dot")]
    if is_sidecar_name(name):
        return [
            fail(
                registry.REF_BAD_FILESET_NAME,
                full,
                f"{name!r} is shaped like a collision sidecar (<name>.remote.<ext>) — "
                "never a real evidence/image file name",
            )
        ]
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return [fail(registry.REF_BAD_FILESET_NAME, full, f"{name!r} is not valid UTF-8")]
    if len(encoded) > _FILESET_NAME_MAX_BYTES:
        return [
            fail(
                registry.REF_BAD_FILESET_NAME,
                full,
                f"{name!r} is {len(encoded)} bytes, over the {_FILESET_NAME_MAX_BYTES}-byte limit",
            )
        ]
    return []


def _check_evidence_extension(folder: PurePosixPath, name: str) -> list[Failure]:
    """``REF-009`` — a file directly inside a report's ``evidence/`` folder must have
    one of Ghostwriter's own allowed extensions (case-insensitive; see
    :mod:`grison.remote.ghostwriter.limits`) — Ghostwriter's server rejects the
    upload of anything else outright. Never applies to a wiki ``images/`` folder
    (BookStack has no such extension restriction) — only reports.py's evidence scan
    calls this. A collision sidecar (``<name>.remote.<ext>``) is never a real file
    grison uploads (``REF-008`` already rejects its shape) so it is exempt here too,
    the same way it is excluded from REF-003's stem comparison."""
    if is_sidecar_name(name):
        return []
    if is_allowed_evidence_extension(name):
        return []
    ext = evidence_extension(name)
    full = f"{folder.as_posix()}/{name}"
    allowed = ", ".join(sorted(EVIDENCE_ALLOWED_EXTENSIONS))
    ext_disp = f".{ext}" if ext else "(none)"
    suggestion = EVIDENCE_EXTENSION_SUGGESTIONS.get(ext)
    hint = f" — convert it to .{suggestion}" if suggestion else ""
    return [
        fail(
            registry.REF_BAD_EVIDENCE_EXTENSION,
            full,
            f"{name!r} has extension {ext_disp!r}, not one of Ghostwriter's allowed "
            f"evidence extensions ({allowed}){hint}",
        )
    ]
