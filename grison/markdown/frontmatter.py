"""One implementation of "split YAML frontmatter from body" and "dump frontmatter +
body" — every hand-written splitter/dumper in the codebase (``document.py``'s
``_split_frontmatter``, ``bsmap.py``'s inline split in ``markdown_to_page``,
``repmap.py``'s inline split in ``read_local_note``, and the five ``yaml.safe_dump``
call sites for the reverse direction) used to do this itself, each slightly
differently. This module is the single source of truth both directions route
through.
"""

from __future__ import annotations

import yaml

from grison.errors import GrisonError

_FENCE = "---"


class DocumentError(GrisonError, ValueError):
    """A document whose YAML frontmatter can't be split from its body, or whose
    frontmatter isn't the shape a caller expects."""


def split(text: str) -> tuple[dict[str, object], str]:
    """Split ``text`` into ``(frontmatter, body)``.

    ``text`` must start with a ``---`` fence; the frontmatter is the YAML between it
    and the next ``---``-only line, parsed as a mapping (an empty block parses as
    ``{}``); ``body`` is everything after the closing fence, verbatim — callers strip
    it themselves if they care. Raises :class:`DocumentError` with a precise reason:
    no opening fence, no closing fence, invalid YAML (with the offending line number
    when pyyaml reports one), or frontmatter that parses but isn't a mapping.
    """
    if not text.startswith(_FENCE):
        raise DocumentError("document has no YAML frontmatter (must start with '---')")
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            break
    else:
        raise DocumentError("unterminated YAML frontmatter (no closing '---')")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        where = _error_location(e)
        raise DocumentError(f"invalid YAML frontmatter{where}: {e}") from e
    if not isinstance(data, dict):
        raise DocumentError("frontmatter is not a mapping")
    return data, body


def _error_location(e: yaml.YAMLError) -> str:
    mark = getattr(e, "problem_mark", None)
    return "" if mark is None else f" (line {mark.line + 1})"


def dump(meta: dict[str, object], body: str) -> str:
    """Render ``meta``/``body`` into a document: ``---\\n<yaml>\\n---\\n\\n<body>\\n``,
    using the exact dump options every writer in the codebase already relied on
    (``sort_keys=False`` — frontmatter field order is deliberate, not alphabetical;
    ``allow_unicode=True`` — author-facing prose isn't ascii-only). ``body`` is
    stripped; an empty body yields a bare frontmatter block with no trailing blank
    body section."""
    fm_yaml = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    stripped = body.strip()
    if stripped:
        return f"{_FENCE}\n{fm_yaml}\n{_FENCE}\n\n{stripped}\n"
    return f"{_FENCE}\n{fm_yaml}\n{_FENCE}\n"


def dump_yaml(data: dict[str, object]) -> str:
    """Plain-YAML sibling of :func:`dump`, same options, for a caller with no
    document body to attach (the methodology structure mirrors — ``.book.yml`` /
    ``.chapter.yml`` — which are YAML-only files, never frontmatter+body)."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
