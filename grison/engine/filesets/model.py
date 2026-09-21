from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from grison.engine.apply import MASS_CHANGE_RATIO
from grison.engine.model import Event, KindSummary, Plan, RemoteRecord, VetoSeverity
from grison.engine.sidecar import is_sidecar_name
from grison.engine.state import StateStore

# One change guard, one pre-write re-fetch guard, one collision-sidecar
# write/clear for every record type (ENGINE.md) — file sets included. Imported
# from grison.engine.apply/grison.engine.sidecar (item 5, fix-fin1) rather than
# re-declared: this module used to carry its own literal copies of the
# threshold/outcome-set constants and the change-guard/sidecar functions, which
# could silently drift from apply.py's if either was edited alone.


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    force_local: frozenset[PurePosixPath] = frozenset()
    force_remote: frozenset[PurePosixPath] = frozenset()
    mass_change_ratio: float = MASS_CHANGE_RATIO
    allow_mass_change: bool = False  # see grison.engine.apply.RunOptions


# --- the file-set adapter protocol + sync loop ------------------------------


class FileSetAdapter(Protocol):
    """One remote file set's binding (an evidence report, or a wiki book's
    gallery) — the file-set counterpart of
    :class:`grison.engine.adapter.Adapter`. ``data`` on every
    :class:`~grison.engine.model.RemoteRecord` this protocol hands back/receives
    is ``{"filename": str, "caption": str, "description": str}`` (bytes are
    fetched separately via :meth:`fetch_body`, so a sync that touches nothing
    never has to download file content for a row it isn't comparing)."""

    kind: str
    supports_caption: bool

    def list_remote(self, ctx: Any) -> dict[int, RemoteRecord]: ...

    def fetch_body(self, ctx: Any, id: int) -> bytes: ...

    def upload(
        self, ctx: Any, *, filename: str, body: bytes, caption: str, description: str
    ) -> RemoteRecord: ...

    def update_caption(
        self, ctx: Any, id: int, *, caption: str, description: str
    ) -> RemoteRecord: ...

    def delete(self, ctx: Any, id: int) -> None: ...

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None: ...

    def restore(self, ctx: Any, preimage: dict[str, Any]) -> RemoteRecord:
        """Undo's inverse of a delete: re-upload ``preimage``'s base64-encoded
        body under its original filename/caption/description (implementations
        should just return :func:`undo_restore`'s result)."""
        ...

    def remote_label(self, data: Any) -> str: ...


@dataclass
class FileSetResult:
    plans: list[Plan]
    events: list[Event]
    summary: KindSummary
    #: filename -> resolved (caption, description) for every file that has one —
    #: what the caller feeds :func:`rewrite_captions` for every referencing
    #: document in scope. Empty when the adapter doesn't support captions.
    resolved_captions: dict[str, tuple[str, str]] = field(default_factory=dict)


def _hash_bytes(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _cached_body_hash(state: StateStore, kind: str, id: int) -> str | None:
    """The remote body's hash as of the last sync that touched this id, from
    ``witness["body_hash"]`` — trustworthy forever, not just until the next
    change: D1 guarantees a file-set record's bytes are immutable for the life
    of its id (replacing bytes always creates a NEW id — see
    ``_apply_reupload``), so once cached this never needs re-verifying by
    download. ``None`` when no cache exists yet (a record indexed before this
    cache existed, or state was lost) — callers fall back to one download to
    establish the baseline."""
    st = state.get(kind, id)
    if st is None:
        return None
    value = st.witness.get("body_hash")
    return value if isinstance(value, str) else None


def _local_files(root: Path, folder: PurePosixPath) -> dict[str, bytes]:
    d = root / folder
    if not d.is_dir():
        return {}
    return {
        p.name: p.read_bytes()
        for p in sorted(d.iterdir())
        if p.is_file() and not p.name.startswith(".") and not is_sidecar_name(p.name)
    }


def _canonical(
    *, body_hash: str, caption: str, description: str, supports_caption: bool
) -> dict[str, Any]:
    if not supports_caption:
        return {"hash": body_hash}
    return {"hash": body_hash, "caption": caption, "description": description}


def caption_only_canonical(data: Mapping[str, Any]) -> dict[str, Any]:
    """The metadata-only canonical payload for a file-set record's caption/
    description alone, with no body hash at all — what
    :class:`grison.engine.adapter.PushUndoAdapter`'s ``canonical_remote`` binding
    for a caption-capable file-set adapter (today only
    :class:`grison.adapters.gw_evidence.GwEvidenceAdapter`) uses to detect drift
    when :mod:`grison.engine.undo` replays a "push" op: a caption/description PUSH
    is the ONLY outcome a file-set adapter ever records as "push" (bytes changing
    is always a re-upload — CREATE + DELETE_REMOTE, see ``_apply_reupload``), and
    D1 guarantees bytes are immutable for the life of an id, so the body hash can
    never be what drifted — comparing it here would only force an unnecessary
    re-download at undo time for a dimension that could never have changed."""
    return {"caption": data.get("caption") or "", "description": data.get("description") or ""}


def _event(
    verb: str,
    *,
    path: PurePosixPath | None,
    label: str | None = None,
    detail: str = "",
    dry_run: bool = False,
    severity: VetoSeverity | None = None,
) -> Event:
    return Event(
        verb=verb,
        path=str(path) if path is not None else None,
        label=label,
        detail=detail,
        dry_run=dry_run,
        severity=severity,
    )
