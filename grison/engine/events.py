"""Uniform event wording (ENGINE.md 'Events'): ``<verb> <path>[ — detail]``, verbs from
a closed list, words never arrow glyphs, a "would " prefix under dry-run. Text and
``--json`` renderers share the same :class:`~grison.engine.model.Event` list so the two
outputs can never drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from grison.engine.model import Event

#: The closed verb list (ENGINE.md). "mirror" covers a read-only structure mirror
#: (re)write; every other verb matches an :class:`~grison.engine.model.Outcome` name
#: (``move_edit`` also renders as "move" — see :func:`grison.engine.apply.plan_event`).
VERBS = frozenset(
    {
        "pull", "push", "create", "delete-remote", "delete-local", "move", "repair",
        "collision", "invalid", "withheld", "skip", "failed", "forget", "mirror",
    }
)


def render_text(event: Event) -> str:
    verb = event.verb
    if verb not in VERBS:
        raise ValueError(f"unknown event verb {verb!r} — not in the closed list {sorted(VERBS)}")
    prefix = "would " if event.dry_run else ""
    line = f"{prefix}{verb}"
    if event.path is not None:
        line += f" {event.path}"
    if event.detail:
        line += f" — {event.detail}"
    return line


def render_text_lines(events: Iterable[Event]) -> list[str]:
    return [render_text(e) for e in events]


def render_json(events: Iterable[Event], *, summary: dict[str, object] | None = None) -> str:
    """One JSON object per event, plus a final summary object — stable keys, one
    object per line (JSON Lines) so a consumer can stream it."""
    lines = []
    for e in events:
        lines.append(
            json.dumps(
                {
                    "type": "event", "verb": e.verb, "path": e.path, "detail": e.detail,
                    "dry_run": e.dry_run,
                },
                sort_keys=True,
            )
        )
    if summary is not None:
        lines.append(json.dumps({"type": "summary", **summary}, sort_keys=True))
    return "\n".join(lines)
