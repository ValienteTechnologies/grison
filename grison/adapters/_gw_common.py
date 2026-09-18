"""Shared Ghostwriter lookups the findings-phase adapters need — never imported by
:mod:`grison.engine` itself (see ``tests/test_engine_no_leak.py``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from grison.index import Index, IndexKind
from grison.remote.ghostwriter import GhostwriterClient


@dataclass
class GWContext:
    """Everything the findings-phase adapters share for one sync run: the live
    client, plus every ``gw.report`` directory currently indexed — a report
    directory's identity comes ONLY from its index entry (D3/BRIEF workspace format
    §1.3), never from parsing an id out of its name."""

    client: GhostwriterClient
    report_dirs: dict[PurePosixPath, int] = field(default_factory=dict)

    @classmethod
    def build(cls, client: GhostwriterClient, index: Index) -> GWContext:
        report_dirs = {
            PurePosixPath(p): rec.id
            for p, rec in index.records.items()
            if rec.kind is IndexKind.GW_REPORT
        }
        return cls(client=client, report_dirs=report_dirs)

    def report_id_for(self, path: PurePosixPath) -> int | None:
        """The report id owning ``path`` (a file or directory anywhere under a
        report directory) — walks up to the nearest indexed ``gw.report`` ancestor."""
        for ancestor in (path, *path.parents):
            rid = self.report_dirs.get(ancestor)
            if rid is not None:
                return rid
        return None


__all__: list[str] = ["GWContext"]
