"""Port protocols — the seams between sources, sinks, and the core.

The seams matter more than the code volume: a *source* turns some external
artifact into ``Finding`` objects, a *sink* writes ``Finding`` objects somewhere.
Adapters (scanners, file sink, remote clients) implement these; the pipeline and
CLI depend only on the protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from grison.model import Finding


@dataclass
class WriteResult:
    """Outcome of a sink write — what landed, what was skipped, what failed."""

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class Source(Protocol):
    """Turns an external artifact (a scanner export, a remote record) into findings."""

    def parse(self, path: Path) -> list[Finding]: ...


@runtime_checkable
class Sink(Protocol):
    """Writes findings somewhere (files today; Ghostwriter/BookStack later)."""

    def write(self, findings: list[Finding], *, dry_run: bool = False) -> WriteResult: ...
