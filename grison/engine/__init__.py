"""The one sync engine (ENGINE.md) — record-type-agnostic core the wiki, reports and
findings phases all move onto, one adapter at a time.

Nothing in this package may know about a specific remote (Ghostwriter/BookStack) or
document kind (page/finding/…) — see ``tests/test_engine_no_leak.py``, which greps the
package for those identifiers. A record type plugs in through
:class:`grison.engine.adapter.Adapter`.
"""

from __future__ import annotations
