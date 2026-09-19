"""The ONE private state layer under ``.grison/state/`` (ENGINE.md 'State').

``.grison/state/<kind>/<id>.json``: ``{"base": "<hash>", "at": "<iso8601>", "witness":
{...}}`` — ``witness`` is whatever cheap server-side change marker the adapter has
(BookStack: ``updated_at`` + ``revision_count``; none on Ghostwriter — an empty dict).
``.grison/state/mirrors.json``: path -> digest, read by
:func:`grison.validator.mirrors.expected_digest` (WS-009). ``.grison/state/last-sync.json``:
the most recent run's summary, read by ``grison status``.

Corrupt or missing state never crashes a sync: every read here returns ``None``/``{}``
on any parse failure, which the classifier already treats as "no base" (a record with
no base classifies by the table).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from grison.fsio import atomic_write_text

STATE_DIR = ".grison/state"
MIRRORS_RELATIVE_PATH = f"{STATE_DIR}/mirrors.json"
LAST_SYNC_RELATIVE_PATH = f"{STATE_DIR}/last-sync.json"


@dataclass(frozen=True)
class RecordState:
    base: str | None
    at: str | None
    witness: dict[str, Any]


class StateStore:
    """Per-record JSON state under ``.grison/state/<kind>/<id>.json``. Single-writer
    (the sync verb holds the workspace flock for its whole run), so plain read/
    atomic-write, no locking of its own."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._dir = root / STATE_DIR

    def _path(self, kind: str, id: int) -> Path:
        return self._dir / kind / f"{id}.json"

    def get(self, kind: str, id: int) -> RecordState | None:
        path = self._path(kind, id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        base = data.get("base")
        at = data.get("at")
        witness = data.get("witness")
        return RecordState(
            base=base if isinstance(base, str) else None,
            at=at if isinstance(at, str) else None,
            witness=witness if isinstance(witness, dict) else {},
        )

    def put(
        self,
        kind: str,
        id: int,
        *,
        base: str | None,
        witness: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        at = at or datetime.now(UTC)
        payload = {"base": base, "at": at.isoformat(), "witness": witness or {}}
        atomic_write_text(self._path(kind, id), json.dumps(payload, sort_keys=True), private=True)

    def forget(self, kind: str, id: int) -> None:
        self._path(kind, id).unlink(missing_ok=True)

    # --- mirrors.json (WS-009) ------------------------------------------------

    def mirrors_path(self) -> Path:
        return self._root / MIRRORS_RELATIVE_PATH

    def load_mirrors(self) -> dict[str, str]:
        path = self.mirrors_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_mirrors(self, mirrors: dict[str, str]) -> None:
        atomic_write_text(
            self.mirrors_path(), json.dumps(mirrors, indent=2, sort_keys=True), private=True
        )

    # --- last-sync.json (grison status) ---------------------------------------

    def save_last_sync(self, payload: dict[str, Any]) -> None:
        path = self._root / LAST_SYNC_RELATIVE_PATH
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True), private=True)

    def load_last_sync(self) -> dict[str, Any] | None:
        path = self._root / LAST_SYNC_RELATIVE_PATH
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
