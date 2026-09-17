"""The read-only mirror pattern (coordinator feedback item 8a) — shared by any
adapter that keeps a generated, git-tracked document over top of a remote record it
deliberately does NOT route through :mod:`grison.engine.classify`/``apply``.

A mirror (today: BookStack's ``.book.yml``/``.chapter.yml``/``.shelves/*.yml``,
written by :mod:`grison.adapters.bs_structure`; tomorrow: whatever the REPORTS
adapter mirrors from Ghostwriter) never collides, is never force-resolved, and is
never "pulled" or "pushed" in the classify/apply sense — its owning adapter decides
on its own (usually from a witness/hash check) WHEN the remote changed enough to
regenerate it, and calls :func:`write_mirror_guarded` to do the write safely. The
rule this module enforces, and enforces in exactly one place:

- if the file doesn't exist yet, write it;
- if it exists and its digest still matches the one grison itself recorded the last
  time it wrote this path, regenerate it (the remote changed, the local copy is
  still exactly what grison last produced);
- if it exists and its digest does NOT match the recorded one, someone hand-edited
  it since — leave it alone. The workspace validator's own rule (WS-009 for the
  wiki case) is the actual enforcement that a hand-edit is invalid, not this write
  path; this function's job is only to never silently clobber one.

``digests`` is the adapter's own persisted ``{rel_path: digest}`` dict (e.g.
``.grison/state/mirrors.json`` via :class:`grison.engine.state.StateStore`) — this
function mutates it on a real write and never persists it itself; the caller decides
when to save.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from grison.fsio import atomic_write_text
from grison.hashing import digest_text


class MirrorWrite(StrEnum):
    WRITTEN = "written"
    WOULD_WRITE = "would_write"  # dry_run=True, otherwise identical to WRITTEN
    UNCHANGED = "unchanged"  # on-disk content already matches ``text``
    HAND_EDITED = "hand_edited"  # left alone; ``message`` explains why


@dataclass(frozen=True)
class MirrorResult:
    outcome: MirrorWrite
    message: str | None = None  # set only for HAND_EDITED


def write_mirror_guarded(
    root: Path, rel_path: str, text: str, digests: dict[str, str], *, dry_run: bool = False,
) -> MirrorResult:
    """Write ``text`` to ``root / rel_path`` unless a hand-edit is detected. See the
    module docstring for the exact rule. Never raises for a hand-edited file — that
    is reported via the returned :class:`MirrorResult`, not an exception, since it is
    an expected, ordinary outcome (a human working in the same file), not a bug."""
    path = root / rel_path
    recorded = digests.get(rel_path)
    if path.exists():
        on_disk = path.read_text(encoding="utf-8")
        if recorded is not None and digest_text(on_disk) != recorded:
            msg = "hand-edited — run `git checkout -- " + rel_path + "` or delete it to regenerate"
            return MirrorResult(MirrorWrite.HAND_EDITED, msg)
        if on_disk == text:
            return MirrorResult(MirrorWrite.UNCHANGED)
    if dry_run:
        return MirrorResult(MirrorWrite.WOULD_WRITE)
    atomic_write_text(path, text)
    digests[rel_path] = digest_text(text)
    return MirrorResult(MirrorWrite.WRITTEN)
