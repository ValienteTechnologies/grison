"""One-time migration helpers — never part of the steady-state sync path.

The v1→v2 workspace migration (id-stripping, ``evidence:`` lists → image lines,
the old-converter body re-derivation this package's docstring used to describe)
has been dropped entirely: D13 now refuses to sync a workspace whose manifest
format differs from the current one, with a message, rather than converting it
in place. The only migration left is :mod:`grison.migrate.wiki_cleanup` (D5) — a
one-time, offline BookStack wiki-body cleanup, run and reviewed by hand, unrelated
to workspace format versioning.
"""

from __future__ import annotations
