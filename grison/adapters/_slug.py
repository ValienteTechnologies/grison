"""The one slugify both remotes' adapters build directory names from.

Ghostwriter (report directory names, D3/D4) and BookStack (page/book/chapter
directory names, D4) each derive a stable, once-only ``[a-z0-9][a-z0-9._-]*``-
shaped slug from a title on pull, and never rename it afterward. The rule is
identical for both — only the empty-input fallback name differs (a report falls
back to "report", a wiki page to "page") — so :mod:`grison.adapters._gw_common`
and :mod:`grison.adapters._bs_common` each keep their own thin, differently-named
``slugify(name)`` wrapping this one shared implementation, rather than
hand-duplicating the regex.
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str, *, fallback: str) -> str:
    """A filesystem-safe slug from ``name`` — ``fallback`` is returned when
    ``name`` slugifies to nothing (e.g. empty or all-punctuation)."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or fallback
