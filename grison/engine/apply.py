"""Backward-compatible shim: the document reconcile engine's real implementation
moved to :mod:`grison.engine.documents` (round 2 dedup — this module used to be
870 lines; see that package's own docstring for the file-by-file split). Every
name this module used to declare is re-exported here, unchanged, so existing
imports (``from grison.engine.apply import run``, ``...RunOptions``,
``...sidecar_path``, ``...change_guard``, ``...refetch_guard``, the
``MASS_CHANGE_*``/``*_WRITE_OUTCOMES`` constants) keep working."""

from __future__ import annotations

from grison.engine.common import LOCAL_WRITE_OUTCOMES as LOCAL_WRITE_OUTCOMES  # re-exported
from grison.engine.common import MASS_CHANGE_MIN as MASS_CHANGE_MIN  # re-exported
from grison.engine.common import MASS_CHANGE_RATIO as MASS_CHANGE_RATIO  # re-exported
from grison.engine.common import REMOTE_WRITE_OUTCOMES as REMOTE_WRITE_OUTCOMES  # re-exported
from grison.engine.common import change_guard as change_guard  # re-exported
from grison.engine.common import refetch_guard as refetch_guard  # re-exported
from grison.engine.documents import RunOptions as RunOptions  # re-exported
from grison.engine.documents import run as run  # re-exported
from grison.engine.sidecar import sidecar_path as sidecar_path  # re-exported

__all__ = [
    "LOCAL_WRITE_OUTCOMES",
    "MASS_CHANGE_MIN",
    "MASS_CHANGE_RATIO",
    "REMOTE_WRITE_OUTCOMES",
    "RunOptions",
    "change_guard",
    "refetch_guard",
    "run",
    "sidecar_path",
]
