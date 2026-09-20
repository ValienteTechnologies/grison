"""Shared primitives every ``grison/formats/*`` module builds on.

Workspace format v2 documents carry no machine fields (no ``grison:`` block, no ids,
no hashes) — tier/kind/location come from the path, not the file. Every model in this
package is a strict pydantic model (``extra="forbid"``) so an unrecognized frontmatter
key is a hard, named failure rather than a silently-dropped author mistake — the
documents are mostly AI-authored, so the format is closed-world by design.

``FormatError`` is the one exception every ``parse()`` in this package raises on a
malformed document. It carries a ``kind`` tag (a short machine-stable string identifying
*which* structural problem occurred — never a rule id: rule ids belong to
:mod:`grison.validator`, the only place that assigns them) plus enough detail (line
number, offending name) for the validator to build a precise, actionable message.
"""

from __future__ import annotations

import re

import yaml
from pydantic import ValidationError

from grison.errors import GrisonError

# Authors may name a new file/directory anything matching this pattern (workspace
# format v2's layout rule) — lowercase ascii, starting alphanumeric, then
# alphanumeric/dot/underscore/hyphen. Applied to a path segment with its extension
# still attached (e.g. "my-finding.md"), so "." is allowed mid-name too.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class FormatError(GrisonError, ValueError):
    """A document failed to parse into its format-v2 model.

    ``kind`` is a short, stable machine tag for the specific structural problem (e.g.
    ``"missing_section"``, ``"unknown_field"``) — :mod:`grison.validator` maps each kind
    to a rule id; nothing in this package ever spells out a rule id itself. ``detail``
    is a free-text fragment naming the offending thing (a field name, a section header,
    a value) for the validator to fold into its message. ``line`` is the 1-based source
    line when known (frontmatter errors and structural body errors carry one; a pydantic
    field error usually doesn't, since YAML frontmatter is parsed as one mapping)."""

    def __init__(self, kind: str, detail: str = "", *, line: int | None = None) -> None:
        self.kind = kind
        self.detail = detail
        self.line = line
        msg = f"{kind}: {detail}" if detail else kind
        super().__init__(msg)


def validate_tags(raw: list[str]) -> list[str]:
    """Shared ``tags`` field-validator body (finding + wiki page): every tag a
    non-empty string with no leading/trailing whitespace, no case-insensitive
    duplicate. Raises plain ``ValueError`` — called from inside a pydantic
    ``field_validator``, which is what turns that into a ``ValidationError`` pydantic
    reports back to the caller (see :func:`translate_validation_error`)."""
    seen: set[str] = set()
    for t in raw:
        if not isinstance(t, str) or not t or t != t.strip():
            raise ValueError(f"invalid tag {t!r}: must be non-empty with no surrounding whitespace")
        key = t.casefold()
        if key in seen:
            raise ValueError(f"duplicate tag {t!r} (case-insensitive)")
        seen.add(key)
    return raw


# Pydantic's own phrasing ("Input should be a valid integer, unable to parse string as
# an integer") is written for a library user reading a traceback, not for an agent
# reading one line of validator output — this maps a pydantic error `type` string to a
# plain description of what grison actually expects, so the message can read
# "<field> is <what was found>; expected <this>" in grison's own words throughout.
_TYPE_DESCRIPTIONS: dict[str, str] = {
    "int_parsing": "an integer",
    "int_type": "an integer",
    "float_parsing": "a number",
    "float_type": "a number",
    "bool_parsing": "true or false",
    "bool_type": "true or false",
    "string_type": "a string",
    "list_type": "a list",
    "dict_type": "a mapping",
    "string_too_short": "a non-empty string",
}


def _clean_value_error(msg: str) -> str:
    """Pydantic prefixes a ``ValueError`` raised inside a field/model validator with
    ``"Value error, "`` — every such message in this codebase is already grison's own
    words (CVSS grammar, unknown CWE, tag shape, tier constraints), so this just
    strips the wrapper pydantic adds around it."""
    return msg.removeprefix("Value error, ")


def _format_enum_expected(expected: str) -> str:
    """Pydantic's ``ctx.expected`` for an ``enum``/``literal_error`` is an
    English-joined, quoted list like ``"'a', 'b' or 'c'"`` — reformat as a plain
    comma-separated list: ``"a, b, c"``."""
    parts = [p.strip().strip("'\"") for p in expected.replace(" or ", ", ").split(",")]
    return ", ".join(p for p in parts if p)


def _find_yaml_key_line(yaml_text: str, loc: tuple[object, ...], *, line_offset: int) -> int | None:
    """1-based line of the (possibly nested) mapping key named by ``loc`` inside
    ``yaml_text``, or ``None`` if it can't be found (the text doesn't parse, or the
    path doesn't lead to a real key — e.g. ``loc`` names a list index, or the error
    has no location at all). ``line_offset`` is how many lines of the FULL document
    precede ``yaml_text`` itself (0 for a fence-less YAML file; 1 for a frontmatter
    block, since the opening ``---`` fence is the document's own line 1)."""
    try:
        node = yaml.compose(yaml_text)
    except yaml.YAMLError:
        return None
    key_node = None
    for key in loc:
        if not isinstance(node, yaml.MappingNode):
            return None
        match = next((kv for kv in node.value if kv[0].value == str(key)), None)
        if match is None:
            return None
        key_node, node = match
    if key_node is None:
        return None
    return int(key_node.start_mark.line) + 1 + line_offset


def find_frontmatter_line(text: str, loc: tuple[object, ...]) -> int | None:
    """1-based line, inside ``text``'s ``---``-fenced YAML frontmatter, of the
    (possibly nested) key named by ``loc`` — best-effort: ``None`` for a document
    with no frontmatter fence, an empty ``loc`` (a model-level error has no single
    field to point at), or a path the YAML structure doesn't actually contain."""
    if not loc or not text.startswith("---"):
        return None
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    return _find_yaml_key_line("\n".join(lines[1:end]), loc, line_offset=1)


def find_yaml_doc_line(text: str, loc: tuple[object, ...]) -> int | None:
    """Same as :func:`find_frontmatter_line`, for a fence-less plain-YAML document
    (a mirror file — ``.report.yml``, ``.book.yml``, etc.) where the whole file IS
    the YAML, starting at line 1."""
    if not loc:
        return None
    return _find_yaml_key_line(text, loc, line_offset=0)


def translate_validation_error(
    e: ValidationError,
    *,
    text: str | None = None,
    fenced: bool = True,
    root_kind_map: dict[str, str] | None = None,
) -> FormatError:
    """Turn a pydantic :class:`ValidationError` into ONE :class:`FormatError`, from its
    first reported error (a document has one first thing wrong; re-validating after a
    fix surfaces the next), with a message in grison's own words rather than
    pydantic's internal phrasing. ``root_kind_map`` maps a substring of a *model-level*
    (``model_validator(mode="after")``) error message to a specific ``kind`` — those
    errors carry no field location (``loc == ()``), so they can only be told apart by
    matching the message a caller wrote; every other error is classified generically
    from its field location: an unrecognized frontmatter key -> ``"unknown_field"``, a
    missing required one -> ``"missing_field"``, anything else -> ``f"bad_{field}"``.
    ``text``, when given, is the document's raw text — used only to look up the
    offending field's source line (``fenced=True`` for ``---``-delimited frontmatter,
    ``False`` for a fence-less plain-YAML document); the line is best-effort and
    ``None`` when it can't be found."""
    root_kind_map = root_kind_map or {}
    first = e.errors()[0]
    loc = first["loc"]
    etype = first["type"]
    msg = first["msg"]
    line = None
    if text is not None:
        line = find_frontmatter_line(text, loc) if fenced else find_yaml_doc_line(text, loc)

    if not loc:
        cleaned = _clean_value_error(msg)
        for needle, kind in root_kind_map.items():
            if needle in cleaned:
                return FormatError(kind, cleaned, line=line)
        return FormatError("invalid", cleaned, line=line)

    field = ".".join(str(p) for p in loc)
    top_field = str(loc[0])
    if etype == "extra_forbidden":
        return FormatError("unknown_field", f"{field} is not a recognized field", line=line)
    if etype == "missing":
        return FormatError("missing_field", f"{field} is required", line=line)
    if etype in ("enum", "literal_error"):
        allowed = _format_enum_expected(first.get("ctx", {}).get("expected", ""))
        got = first.get("input")
        detail = f"{field} is {got!r}; allowed: {allowed}" if allowed else f"{field} is {got!r}"
        return FormatError(f"bad_{top_field}", detail, line=line)
    if etype == "value_error":
        return FormatError(f"bad_{top_field}", f"{field}: {_clean_value_error(msg)}", line=line)
    expected = _TYPE_DESCRIPTIONS.get(etype)
    if expected is not None:
        got = first.get("input")
        return FormatError(
            f"bad_{top_field}", f"{field} is {got!r}; expected {expected}", line=line
        )
    return FormatError(f"bad_{top_field}", f"{field}: {msg}", line=line)
