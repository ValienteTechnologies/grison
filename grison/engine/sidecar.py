"""Collision-sidecar naming (ENGINE.md §8) — the one convention, kept in its own
leaf module (no dependency on anything else in ``grison`` beyond the record-type-
agnostic ``grison.engine.model``/``grison.fsio``) so it can be reached from both
directions of an otherwise-circular import: :mod:`grison.engine.apply`
(which pulls in :mod:`grison.validator.registry`, and therefore the whole
``grison.validator`` package's ``__init__``, via ``Failure``) and
:mod:`grison.validator.core` (item 5: excluding a live sidecar from the
evidence/images directory scans needs the exact same rule ``grison status``'s
sidecar-aware counts and :mod:`grison.engine.filesets`'s local scan already use).
``grison.engine.apply`` re-exports :func:`sidecar_path` (existing callers —
:mod:`grison.engine.offline_status` — keep importing it from there); every other
caller of either function (:mod:`grison.engine.filesets`, :mod:`grison.cli`,
:mod:`grison.validator.core`) imports straight from this module.

:func:`write_sidecar`/:func:`clear_stale_sidecars` are the ONE collision-sidecar
write/clear (ENGINE.md §8), shared by :mod:`grison.engine.apply` and
:mod:`grison.engine.filesets` — the two record shapes (a document's text vs. a
file set's raw bytes) differ only in how the remote version's bytes are produced,
never in the write-atomically-or-not-at-all/clear-when-no-longer-colliding
mechanics, so those two callers each supply their own ``render``/pass their own
``plans`` and get the exact same behavior."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from grison.engine.model import Outcome, Plan
from grison.fsio import atomic_write_bytes


def sidecar_path(path: PurePosixPath) -> PurePosixPath:
    """The collision-sidecar path for ``path`` (``page.md`` -> ``page.remote.md``,
    an extension-less ``README`` -> ``README.remote``)."""
    if path.suffix:
        return path.with_suffix(f".remote{path.suffix}")
    return path.with_name(path.name + ".remote")


def is_sidecar_name(name: str) -> bool:
    """True for a bare filename shaped like :func:`sidecar_path` would produce from
    some OTHER name in the same directory (``shot.png`` -> ``shot.remote.png``, an
    extension-less ``README`` -> ``README.remote``) — the inverse check, by shape
    alone (no sibling file needs to exist). Every directory listing that must not
    mistake a live collision sidecar for a real record needs the exact same rule —
    :mod:`grison.engine.filesets` (a file-set sidecar's extension varies with the
    file it shadows, unlike a document sidecar's fixed ``.md``, so a literal
    ``.remote.md`` check would miss it), ``grison status``'s evidence/image file
    counts, and :mod:`grison.validator.core`'s evidence/images directory scans all
    call this rather than re-deriving it."""
    p = PurePosixPath(name)
    if p.suffix == ".remote":
        return True
    return PurePosixPath(p.stem).suffix == ".remote"


def write_sidecar(
    root: Path,
    path: PurePosixPath | None,
    remote: object | None,
    render: Callable[[], bytes],
) -> None:
    """The ONE collision-sidecar write (ENGINE.md §8): a no-op when there is no
    local path to sidecar next to, or no remote record to render from (e.g. "edited
    locally, deleted remotely" — nothing left on the server to show); otherwise
    calls ``render`` (the caller's own way to produce the remote version's bytes —
    a document adapter's ``render_local(...).encode()``, a file-set adapter's
    ``fetch_body``) and writes the result atomically at :func:`sidecar_path`.
    Callers decide when (or whether) to call this at all — dry-run gating is the
    caller's responsibility, not this function's."""
    if path is None or remote is None:
        return
    atomic_write_bytes(root / sidecar_path(path), render())


def clear_stale_sidecars(root: Path, plans: list[Plan]) -> None:
    """ENGINE.md §8: a sidecar is cleared as soon as its record is no longer in
    collision (resolved by a force flag, or the two sides converged)."""
    for p in plans:
        if p.path is None or p.outcome is Outcome.COLLISION:
            continue
        (root / sidecar_path(p.path)).unlink(missing_ok=True)
