"""The Adapter protocol (ENGINE.md 'Adapter protocol') — one implementation per
record type. The engine only ever calls these methods; it never imports a concrete
remote client or document format itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, runtime_checkable

from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto

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

    def canonical_remote(self, data: Any) -> Canonical: ...

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

    def create(self, ctx: Any, doc: Any) -> RemoteRecord: ...

    def update(self, ctx: Any, id: int, doc: Any) -> RemoteRecord: ...

    def delete(self, ctx: Any, id: int) -> None: ...

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord:
        """Undo's inverse of :meth:`delete` — recreate a deleted record from its
        captured pre-image (adapter-defined shape)."""
        ...

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        """A :class:`~grison.engine.model.Veto` if this record must be SKIPped
        regardless of what the classification table says (a server-side condition the
        table can't express — ENGINE.md: a non-markdown editor, a recycle-bin record,
        a record moved to another parent on the server), else ``None``.

        Severity rule (ENGINE.md exit-code policy extended to SKIP): use
        ``VetoSeverity.INFO`` when there is nothing the user must do about it — the
        record is inert by its own nature (a draft, a template) regardless of
        anything grison or the user did. Use ``VetoSeverity.ATTENTION`` when the veto
        blocks something grison actually manages or was asked to do — a record the
        index already tracks (``local is not None``, or the record is indexed at all)
        whose remote copy became unusable (flipped to wysiwyg, landed in the recycle
        bin, moved to a parent grison can't resolve). The same underlying condition
        (e.g. a wysiwyg editor) can be either: a wysiwyg page grison has never seen
        before is INFO (nothing lost, nothing to do); a wysiwyg page that used to be
        markdown and blocks a pending local edit is ATTENTION.
        """
        ...

    def remote_label(self, data: Any) -> str:
        """A human-readable identifier for a remote record with no local path yet —
        e.g. a vetoed PULL_NEW candidate. Every event names its record (path when
        there is one, this label otherwise); an adapter that can be vetoed before a
        local path exists must implement this so that never degrades to a bare,
        unidentifiable "skip — <reason>" line. Conventionally ``'"<title>" (<noun>
        <id>)'``, e.g. ``'"WYSIWYG Created Page" (page 28)'``."""
        ...


@runtime_checkable
class CreateUndoAdapter(Protocol):
    """The subset of :class:`Adapter` needed to undo a CREATE — refetch the record
    (to confirm it's still exactly what was created) and delete it. Every full
    ``Adapter`` already satisfies this structurally; a record kind whose only
    ever-recorded undo op is a create (a structure adapter that creates a parent
    directory's remote counterpart but never pushes/deletes it through the engine —
    :class:`grison.adapters.bs_structure.BookUndoAdapter`/``ChapterUndoAdapter``) can
    implement *just* this, with no ``restore`` stub: the undo engine only ever calls
    ``restore`` on an op it knows requires it (PUSH/MOVE_EDIT/DELETE_REMOTE), and only
    those adapters need to satisfy :class:`RestorableUndoAdapter` — the impossible
    call (asking a create-only adapter to restore a pre-image it never captured) is
    unrepresentable rather than a method that exists only to raise.
    """

    kind: str

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None: ...

    def delete(self, ctx: Any, id: int) -> None: ...


@runtime_checkable
class RestorableUndoAdapter(CreateUndoAdapter, Protocol):
    """:class:`CreateUndoAdapter` plus ``restore`` — required for undoing a
    PUSH/MOVE_EDIT/DELETE_REMOTE op (anything with a captured remote pre-image)."""

    def restore(self, ctx: Any, preimage: Any) -> RemoteRecord: ...


@runtime_checkable
class PushUndoAdapter(RestorableUndoAdapter, Protocol):
    """:class:`RestorableUndoAdapter` plus ``canonical_remote`` — required to undo
    a PUSH/MOVE_EDIT op specifically: unlike CREATE (existence-only: "is it still
    there?") and DELETE_REMOTE (existence-only: "has something already taken this
    identity?"), undoing a push must detect a record that was edited again, in
    place, after the write being undone — a content check, not an existence
    check — before overwriting it with the older pre-image
    (:mod:`grison.engine.undo`'s module docstring; ENGINE.md §3's guard, extended
    to undo). Every adapter that ever records a push/move_edit
    :class:`~grison.engine.undo.UndoOp` already has ``canonical_remote`` (it is
    part of the full :class:`Adapter` protocol every document adapter
    implements; a caption-capable file-set adapter — today only
    :class:`grison.adapters.gw_evidence.GwEvidenceAdapter` — adds its own undo-
    only binding, see :func:`grison.engine.filesets.caption_only_canonical`). An
    adapter that never records that outcome (BookStack's image gallery, whose
    ``update_caption`` is unreachable — D9: no caption column at all) need not
    implement this beyond satisfying :class:`RestorableUndoAdapter`."""

    def canonical_remote(self, data: Any) -> Canonical: ...
