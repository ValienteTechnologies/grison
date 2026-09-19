"""Collision-sidecar naming (ENGINE.md §8) — the one convention, kept in its own
leaf module (no dependency on anything else in ``grison``) so it can be reached
from both directions of an otherwise-circular import: :mod:`grison.engine.apply`
(which pulls in :mod:`grison.validator.registry`, and therefore the whole
``grison.validator`` package's ``__init__``, via ``Failure``) and
:mod:`grison.validator.core` (item 5: excluding a live sidecar from the
evidence/images directory scans needs the exact same rule ``grison status``'s
sidecar-aware counts and :mod:`grison.engine.filesets`'s local scan already use).
``grison.engine.apply`` re-exports :func:`sidecar_path` (existing callers —
:mod:`grison.engine.offline_status` — keep importing it from there); every other
caller of either function (:mod:`grison.engine.filesets`, :mod:`grison.cli`,
:mod:`grison.validator.core`) imports straight from this module."""

from __future__ import annotations

from pathlib import PurePosixPath


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
