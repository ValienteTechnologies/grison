"""The seam between the converter and grison's evidence/wiki-image bookkeeping.

An embedded image (``![caption](evidence/file.png "description")``) and a
cross-reference link (``[text](evidence/file.png)``) name a *local workspace path*.
Ghostwriter's rich-text HTML instead names a *remote identity* — an evidence row id
or a bookmark/reference name — and carries no caption/description of its own (those
live on the evidence row, synced separately; see D1 in the workspace format spec).
``RefResolver`` is the two-way lookup a caller plugs into
:mod:`grison.markdown.converter` so the converter can translate between the two
without ever touching a filesystem, an index file, or a network — it only calls
``to_remote``/``to_local`` once per reference it meets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class RemoteRef:
    """A resolved remote identity for an embedded image or a cross-reference target.

    ``kind`` distinguishes the two remote systems D1/D9 let a reference point into.
    This converter's HTML side only ever produces/consumes ``"gw-evidence"`` —
    BookStack pages are markdown-native, so a wiki image reference (D9) never
    becomes an HTML construct — but ``"bs-image"`` is a valid value so the one
    dataclass serves both adapters of the shared "a folder mirrors a remote file
    set; an image line is a reference into it" mechanism.

    ``id`` is the evidence row id (load-bearing for the native embed div). ``name``
    is the friendly/bookmark-style name Ghostwriter's legacy dot-syntax and its
    cross-reference span key off (``friendlyName`` for an embed, an arbitrary
    reference name for a cross-reference — see the converter's module docstring).
    ``url`` is carried for a resolver's own bookkeeping; the converter never reads
    it. All fields but ``kind`` may be ``None`` when not known/needed for the
    reference at hand (an embed needs ``id``; a cross-reference needs ``name``)."""

    kind: Literal["gw-evidence", "bs-image"]
    id: int | None
    name: str | None
    url: str | None


@dataclass(frozen=True)
class LocalRef:
    """What ``html_to_md`` needs to render a resolved reference back to markdown.

    ``path`` is the workspace-relative file path (D1's identity — what actually
    appears in the markdown image/link line). ``caption``/``description`` are the
    evidence row's own fields, used for an embed's ``![caption](path "description")``
    and (as ``caption``, falling back to the path's stem when empty) for a
    cross-reference's link text. Both default to ``""`` — never ``None`` — so a
    resolver that has nothing to offer for a field it wasn't asked to fill (e.g. a
    plain cross-reference lookup only cares about ``path``) doesn't have to invent
    a sentinel."""

    path: str
    caption: str = ""
    description: str = ""


class RefResolver(Protocol):
    """Implementations do the real filesystem/index lookups (D1/D9's evidence-row
    <-> workspace-path bookkeeping); the converter only ever calls these two
    methods, never touches disk or the network itself, and never inspects a
    workspace's structure directly."""

    def to_remote(self, path: str) -> RemoteRef | None:
        """``path`` exactly as written in a markdown image/link line -> the remote
        identity to embed on push, or ``None`` if grison has no record of that path
        (a push-time validation failure — the converter raises ``ConverterError``,
        naming the path, rather than emitting HTML with a missing identity)."""
        ...

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        """A remote identity recovered from pulled HTML (an embed div's id, a
        legacy friendly name, a cross-reference span's name) -> its local
        workspace path (+ caption/description for an embed), or ``None`` if grison
        can't resolve it locally (e.g. the evidence hasn't been synced down yet).
        On ``None``, ``html_to_md`` keeps the reference as an inert, visible
        "unresolved reference" placeholder instead of failing the whole document —
        see :mod:`grison.markdown.converter`'s module docstring."""
        ...
