"""Rollback safety net — every remote write batch is snapshot-backed.

As the sync engine writes, it records the inverse of each mutation (the pre-image of
an update, or a delete for an insert/upload). The batch can then be rolled back in
process, and is also persisted (``undo.json`` + a ``rollback.py``) under
``~/.local/share/grison/snapshots/`` so a write is reversible even long after the run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grison.remote.ghostwriter import GhostwriterClient

SNAPSHOT_ROOT = Path.home() / ".local" / "share" / "grison" / "snapshots"


@dataclass
class Undo:
    # op ∈ {update_finding, update_reportedFinding, delete_finding,
    #       delete_reportedFinding, delete_evidence, upload_evidence}
    op: str
    id: int
    fields: dict | None = None


@dataclass
class Snapshot:
    """Accumulates the inverse of a write batch; can roll back and persist."""

    undos: list[Undo] = field(default_factory=list)

    def before_update(self, table: str, record_id: int, pre_image: dict) -> None:
        op = "update_finding" if table == "finding" else "update_reportedFinding"
        self.undos.append(Undo(op, record_id, pre_image))

    def after_insert(self, table: str, record_id: int) -> None:
        op = "delete_finding" if table == "finding" else "delete_reportedFinding"
        self.undos.append(Undo(op, record_id))

    def after_upload_evidence(self, evidence_id: int) -> None:
        self.undos.append(Undo("delete_evidence", evidence_id))

    def before_delete_evidence(
        self,
        evidence_id: int,
        finding_id: int,
        filename: str,
        caption: str,
        friendly_name: str,
        file_base64: str,
    ) -> None:
        """The pre-image of an evidence row about to be deleted. Ghostwriter's evidence
        API has no update/restore-in-place, so undoing a delete means re-uploading the
        captured bytes as a new row — ``fields`` matches ``upload_evidence``'s kwargs
        exactly so rollback can splat it straight through."""
        self.undos.append(
            Undo(
                "upload_evidence",
                evidence_id,
                fields={
                    "finding_id": finding_id,
                    "filename": filename,
                    "caption": caption,
                    "friendly_name": friendly_name,
                    "file_base64": file_base64,
                },
            )
        )

    @property
    def empty(self) -> bool:
        return not self.undos

    def rollback(self, client: GhostwriterClient) -> None:
        """Undo every recorded mutation, in reverse order."""
        for u in reversed(self.undos):
            if u.op == "update_finding":
                client.update_finding(u.id, u.fields or {})
            elif u.op == "update_reportedFinding":
                client.update_reported_finding(u.id, u.fields or {})
            elif u.op == "delete_finding":
                client.delete_finding(u.id)
            elif u.op == "delete_reportedFinding":
                client.delete_reported_finding(u.id)
            elif u.op == "delete_evidence":
                client.delete_evidence(u.id)
            elif u.op == "upload_evidence":
                client.upload_evidence(**(u.fields or {}))

    def persist(self, when: str) -> Path:
        """Write ``undo.json`` + a runnable ``rollback.py`` under a per-batch dir."""
        out = SNAPSHOT_ROOT / when
        out.mkdir(parents=True, exist_ok=True)
        (out / "undo.json").write_text(
            json.dumps([asdict(u) for u in self.undos], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out / "rollback.py").write_text(_ROLLBACK_SCRIPT, encoding="utf-8")
        return out


_ROLLBACK_SCRIPT = '''\
#!/usr/bin/env python3
"""Roll back the grison write batch recorded in undo.json (this dir).

Usage: python rollback.py [WORKSPACE_DIR]   # default: cwd; needs .grison/env or GRISON_* env
"""
import json
import sys
from pathlib import Path

from grison.remote.creds import load
from grison.remote.ghostwriter import GhostwriterClient
from grison.remote.snapshot import Snapshot, Undo

here = Path(__file__).parent
undos = [Undo(**u) for u in json.loads((here / "undo.json").read_text(encoding="utf-8"))]
root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
creds = load(root)
creds.require_ghostwriter()
with GhostwriterClient(creds) as client:
    Snapshot(undos=undos).rollback(client)
print(f"rolled back {len(undos)} action(s)")
'''
