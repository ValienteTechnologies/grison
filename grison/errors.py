"""The one base class every grison exception derives from.

Callers that need to tell "grison refused / a server refused" apart from a programming
error catch :class:`GrisonError`; the CLI turns it into a plain message and exit code 1
instead of a traceback.
"""

from __future__ import annotations


class GrisonError(Exception):
    """Base of every exception grison raises on purpose."""
