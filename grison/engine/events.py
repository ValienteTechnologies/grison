"""Uniform event wording (ENGINE.md 'Events'): ``<subject>[ — detail]``, verbs from a
closed list, words never arrow glyphs, a "would " prefix under dry-run. Text and
``--json`` renderers share the same :class:`~grison.engine.model.Event` list so the two
outputs can never drift apart.

:func:`build_event` is the ONE place an :class:`~grison.engine.model.Event` gets built
from a :class:`~grison.engine.model.Plan` — it resolves the record's identity (local
path, or the adapter's ``remote_label`` when there is none) so no call site can forget
to and reproduce the "skip — remote page is not markdown-native …" bug (an event
naming nothing) that motivated this factory.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from grison.engine.model import Event, VetoSeverity

if TYPE_CHECKING:
    from grison.engine.adapter import Adapter
    from grison.engine.model import Plan

#: The closed verb list (ENGINE.md). "mirror" covers a read-only structure mirror
#: (re)write; "loss" is an INFO-severity side-channel event (see :func:`emit_losses`)
#: for a converter's ``on_loss`` message, never tied to a Plan's own outcome; every
#: other verb matches an :class:`~grison.engine.model.Outcome` name (``move_edit``
#: also renders as "move" — see :func:`grison.engine.apply.plan_event`).
VERBS = frozenset(
    {
        "pull", "push", "create", "delete-remote", "delete-local", "move", "repair",
        "collision", "invalid", "withheld", "skip", "failed", "forget", "mirror", "loss",
    }
)

#: Outcome.value -> event verb, for anywhere (apply.py's own event construction,
#: undo.py's snapshot summaries) that needs to name an outcome's verb consistently.
#: Falls back to swapping underscores for hyphens for anything not listed (already
#: the right shape for e.g. "delete_local" -> "delete-local").
_OUTCOME_VERB = {"move_edit": "move", "delete_remote": "delete-remote"}


def verb_for_outcome(outcome: str) -> str:
    return _OUTCOME_VERB.get(outcome, outcome.replace("_", "-"))


def build_event(
    verb: str, plan: Plan, adapter: Adapter, *, detail: str = "", dry_run: bool = False,
) -> Event:
    """Build the event for ``plan``, resolving its identity generically: ``plan.path``
    when present, else ``adapter.remote_label(plan.remote.data)`` (a record discovered
    only on the server — e.g. a vetoed PULL_NEW candidate — has no local path yet)."""
    path = str(plan.path) if plan.path is not None else None
    label = None
    if path is None:
        label = adapter.remote_label(plan.remote.data) if plan.remote is not None else (
            f"{plan.kind} {plan.id}" if plan.id is not None else None
        )
    return Event(verb=verb, path=path, label=label, detail=detail, dry_run=dry_run,
                severity=plan.severity)


def emit_losses(events: list[Event], path: str, losses: Iterable[str]) -> None:
    """Append an INFO-severity ``loss`` event for each entry in ``losses`` (a
    converter's ``on_loss`` messages — a dropped/canonicalized construct on an
    HTML->markdown conversion), de-duplicated per ``path`` within ``events`` itself
    — an adapter may recompute the same record's conversion more than once in one
    sync (e.g. once in ``fetch_remote``, again if the pre-write re-fetch guard
    fires), and a genuinely dropped construct should surface once per sync, not
    once per internal recomputation. Any adapter that converts remote HTML to local
    text calls this wherever it writes that text to disk — the ONE hook every such
    adapter (report sections, notes, and eventually findings) shares."""
    seen = {(e.path, e.detail) for e in events if e.verb == "loss"}
    for msg in losses:
        if (path, msg) in seen:
            continue
        events.append(Event(verb="loss", path=path, detail=msg, severity=VetoSeverity.INFO))
        seen.add((path, msg))


def render_text(event: Event) -> str:
    verb = event.verb
    if verb not in VERBS:
        raise ValueError(f"unknown event verb {verb!r} — not in the closed list {sorted(VERBS)}")
    prefix = "would " if event.dry_run else ""
    line = f"{prefix}{verb} {event.subject}"
    if event.detail:
        line += f" — {event.detail}"
    return line


def render_text_lines(events: Iterable[Event], *, verbose: bool = False) -> list[str]:
    """Text rendering hides INFO-severity events (nothing the user must do — a draft/
    template page) unless ``verbose``; ATTENTION and unscored events always show."""
    return [
        render_text(e) for e in events if verbose or e.severity is not VetoSeverity.INFO
    ]


def render_json(events: Iterable[Event], *, summary: Mapping[str, object] | None = None) -> str:
    """One JSON object per event (ALWAYS all of them, including INFO-severity —
    ``--json`` is for machine consumers that can filter for themselves), plus a final
    summary object — stable keys, one object per line (JSON Lines) so a consumer can
    stream it."""
    lines = []
    for e in events:
        lines.append(
            json.dumps(
                {
                    "type": "event", "verb": e.verb, "path": e.path, "label": e.label,
                    "detail": e.detail, "dry_run": e.dry_run,
                    "severity": e.severity.value if e.severity is not None else None,
                },
                sort_keys=True,
            )
        )
    if summary is not None:
        lines.append(json.dumps({"type": "summary", **summary}, sort_keys=True))
    return "\n".join(lines)
