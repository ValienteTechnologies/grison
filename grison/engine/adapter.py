"""The Adapter protocol (ENGINE.md 'Adapter protocol') — one implementation per
record type. The engine only ever calls these methods; it never imports a concrete
remote client or document format itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from grison.engine.model import Canonical, LocalDoc, RemoteRecord

AdapterMode = Literal["read-write", "append-only", "read-only"]


class Adapter(Protocol):
    """One record type's binding into the engine. ``kind`` is the
    :class:`grison.index.IndexKind` value this adapter owns (e.g. ``"bs.page"`` —
    this module never imports that enum itself, to stay remote-agnostic)."""

    kind: str
    mode: AdapterMode

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        """Every local document of this kind under ``root``, parsed via
        ``grison.formats``. Never raises on one bad file — a parse failure is
        reported as an event by the caller, not by the adapter."""
        ...

    def fetch_remote(self, ctx: Any) -> dict[int, RemoteRecord]:
        """Every remote record of this kind, bulk-fetched, keyed by id."""
        ...

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None:
        """Re-fetch exactly one record (the pre-write re-fetch guard). ``None`` if it
        is gone."""
        ...

    def canonical_local(self, doc: Any) -> Canonical:
        """The hashable payload for a local document — symmetric with
        :meth:`canonical_remote`: the same field, from either side, must reduce to
        the same JSON shape so ``grison.hashing.digest`` agrees when content agrees."""
        ...

    def canonical_remote(self, data: Any) -> Canonical:
        ...

    def render_local(self, data: Any, *, path: PurePosixPath) -> str:
        """The exact file text to write for a pulled/created record (``grison.formats``
        dump)."""
        ...

    def default_path(self, data: Any, *, root: Path) -> PurePosixPath:
        """Where a newly-pulled record's file goes: ``slug(title)`` inside the parent
        directory the remote data implies, de-duplicated ``-2``, ``-3`` … within that
        directory."""
        ...

    def relocated_path(self, data: Any, *, current: PurePosixPath) -> PurePosixPath:
        """Where an already-indexed record's file should live given fresh remote
        data, keeping ``current``'s filename — the PULL-side counterpart of a local
        move: if the record's remote parent changed (a page moved to another chapter
        on the server), the file must physically relocate on pull, or the very next
        sync would see the directory disagree with the fresh content and push it
        straight back (a ping-pong). Adapters with no location concept of their own
        just return ``current`` unchanged."""
        ...

    def create(self, ctx: Any, doc: Any) -> RemoteRecord:
        ...

    def update(self, ctx: Any, id: int, doc: Any) -> RemoteRecord:
        ...

    def delete(self, ctx: Any, id: int) -> None:
        ...

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord:
        """Undo's inverse of :meth:`delete` — recreate a deleted record from its
        captured pre-image (adapter-defined shape)."""
        ...

    def veto(self, local: Any | None, remote: Any | None) -> str | None:
        """A reason string if this record must be SKIPped regardless of what the
        classification table says (a server-side condition the table can't express —
        ENGINE.md: a non-markdown editor, a recycle-bin record, a record moved to
        another parent on the server), else ``None``."""
        ...


class UndoAdapter(Protocol):
    """The subset of :class:`Adapter` :mod:`grison.engine.undo` actually calls —
    every full ``Adapter`` already satisfies this structurally. A record kind whose
    only ever-recorded undo op is a create (e.g. a structure adapter that creates a
    parent directory's remote counterpart but never pushes/deletes it through the
    engine) may implement just this smaller shape instead of the full protocol."""

    kind: str

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None: ...

    def delete(self, ctx: Any, id: int) -> None: ...

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord: ...
