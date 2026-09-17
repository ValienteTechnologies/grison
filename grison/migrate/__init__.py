"""One-time workspace migration helpers — never part of the steady-state sync
path. See :mod:`grison.migrate.bodies` for the markdown-body migration
(:func:`~grison.migrate.bodies.migrate_body_v1`) that re-derives a workspace's
existing markdown bodies through the old (frozen, ``grison/migrate/_v1_converter.py``)
converter and then the current one, so already-synced content — written when
grison's own ``html_to_md`` output wasn't always valid CommonMark — reads back
identically under the new, real-CommonMark-parsing converter instead of being
silently misread or hard-rejected.
"""

from __future__ import annotations

from grison.migrate.bodies import BodyMigrationError, migrate_body_v1

__all__ = ["BodyMigrationError", "migrate_body_v1"]
