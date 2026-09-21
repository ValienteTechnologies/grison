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
import unicodedata

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Letters with no Unicode decomposition (NFKD leaves them alone), folded the way
# BookStack's slugger (Laravel ``Str::slug`` -> ``Str::ascii``) folds them, so a
# title slugged here lands on the same slug BookStack would give it. Public so
# the test fake of BookStack folds with the SAME table (one table, one truth).
FOLD = str.maketrans(
    {
        "ı": "i",
        "ß": "ss",
        "æ": "ae",
        "œ": "oe",
        "ø": "o",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ł": "l",
        "ŧ": "t",
        "ħ": "h",
        "ĸ": "k",
        "ŋ": "n",
        "ſ": "s",
    }
)


def slugify(name: str, *, fallback: str) -> str:
    """A filesystem-safe ``[a-z0-9][a-z0-9-]*`` slug from ``name``: lower-cased,
    transliterated to ASCII (accents/diacritics dropped via NFKD, plus the
    non-decomposable letters in ``_FOLD``), every other run of characters
    collapsed to one ``-``. ``fallback`` is returned when ``name`` slugifies to
    nothing (e.g. empty or all-punctuation). ``"Sızma Testi Raporu"`` ->
    ``"sizma-testi-raporu"``, never ``"s-zma-testi-raporu"``."""
    folded = unicodedata.normalize("NFKD", name.strip().lower()).translate(FOLD)
    ascii_text = "".join(c for c in folded if not unicodedata.combining(c))
    slug = _SLUG_RE.sub("-", ascii_text).strip("-")
    return slug or fallback
