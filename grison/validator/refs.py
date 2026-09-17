"""Offline reference resolution for the validator: a filesystem-backed ``RefResolver``
plus thin, path-prefix-filtered views over :func:`grison.markdown.refscan.scan_refs`
for the cross-document checks (REF-003 stem collisions, REF-004 caption conflicts)
that a per-field/per-page converter call can't see on its own — it converts one field
at a time with no memory of what any other document said about the same file.

:class:`OfflineEvidenceResolver` is the ``RefResolver`` (:mod:`grison.markdown.refs`)
the validator plugs into ``md_to_html`` so a finding's or narrative section's body can
still be validated for everything ELSE (structure, unsupported constructs) with no
network — it only ever answers the PUSH direction (``to_remote``); ``to_local`` is
never called by ``md_to_html``, so it is a stub.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.markdown.refs import LocalRef, RefResolver, RemoteRef
from grison.markdown.refscan import FoundRef, scan_refs


class OfflineEvidenceResolver:
    """Resolves ``evidence/<file>`` against one report's ``evidence/`` folder."""

    def __init__(self, evidence_dir: Path) -> None:
        self._dir = evidence_dir

    def to_remote(self, path: str) -> RemoteRef | None:
        if not path.startswith("evidence/"):
            return None
        fname = path[len("evidence/") :]
        if not (self._dir / fname).is_file():
            return None
        return RemoteRef("gw-evidence", id=1, name=PurePosixPath(fname).stem, url=None)

    def to_local(self, remote: RemoteRef) -> LocalRef | None:  # pragma: no cover — pull-only
        del remote
        return None


def embeds(md: str, *, prefix: str) -> list[FoundRef]:
    """Every embed (``![...](...)``) whose path starts with ``<prefix>/`` — real
    references only: one whose destination sits inside a fenced/inline code block was
    never tokenized as an image by markdown-it in the first place (see
    :mod:`grison.markdown.refscan`'s module docstring), so it can never appear here."""
    return [r for r in scan_refs(md) if r.kind == "embed" and r.path.startswith(prefix + "/")]


def cross_refs(md: str, *, prefix: str) -> list[FoundRef]:
    """Every cross-reference link (``[...](...)``, not an embed) whose path starts
    with ``<prefix>/``."""
    return [
        r for r in scan_refs(md) if r.kind == "cross_reference" and r.path.startswith(prefix + "/")
    ]


def wiki_images(md: str) -> list[FoundRef]:
    """Every embed shaped like a wiki image reference — either the book-root spelling
    (``images/<file>``) or the in-chapter spelling (``../images/<file>``); REF-007
    decides which one is actually correct for a given page's location, this only
    recognizes the shape."""
    return [
        r for r in scan_refs(md)
        if r.kind == "embed" and (r.path.startswith("images/") or r.path.startswith("../images/"))
    ]


def stems(filenames: list[str]) -> dict[str, list[str]]:
    """Group filenames by stem (name without extension) — REF-003's raw material."""
    groups: dict[str, list[str]] = {}
    for name in filenames:
        groups.setdefault(PurePosixPath(name).stem, []).append(name)
    return groups


__all__ = [
    "FoundRef",
    "OfflineEvidenceResolver",
    "RefResolver",
    "cross_refs",
    "embeds",
    "stems",
    "wiki_images",
]
