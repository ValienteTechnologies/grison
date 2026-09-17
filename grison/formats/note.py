"""Format v2 project note documents: ``findings/reports/<dir>/notes/*.md``.

Two shapes share the directory, told apart WITHOUT any id in the file — purely by
whether the path is in ``.grison/index.json`` (D3): a **mirrored note** (an existing
Ghostwriter ``projectNote``, regenerated read-only every sync — see
:mod:`grison.validator.mirrors`) carries a small frontmatter block (``author``,
``timestamp`` — both optional, both display metadata, neither an identity field); a
**new local note** (not yet pushed) is bare markdown with no frontmatter fence at all.
This module parses either shape structurally (by whether the text starts with the
frontmatter fence); it is :mod:`grison.validator` that knows the index and enforces
which shape a given path is allowed to have (REP-002).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from grison.formats.common import FormatError, translate_validation_error
from grison.markdown import frontmatter as fm
from grison.markdown.frontmatter import DocumentError


class _NoteFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str | None = None
    timestamp: str | None = None


class NoteDoc(BaseModel):
    """A parsed note, either shape. ``has_frontmatter`` records which shape the text
    was actually written in — :mod:`grison.validator` compares it against index
    membership (REP-002); ``author``/``timestamp`` are ``None`` for a new local note."""

    model_config = ConfigDict(extra="forbid")

    author: str | None = None
    timestamp: str | None = None
    body: str = ""
    has_frontmatter: bool = False


def parse(text: str, *, path: Path) -> NoteDoc:
    del path
    if not text.startswith("---"):
        return NoteDoc(body=text.strip(), has_frontmatter=False)
    try:
        meta, body = fm.split(text)
    except DocumentError as e:
        raise FormatError("bad_frontmatter", str(e)) from e
    try:
        fm_doc = _NoteFrontmatter.model_validate(meta)
    except ValidationError as e:
        raise translate_validation_error(e, text=text) from e
    return NoteDoc(
        author=fm_doc.author, timestamp=fm_doc.timestamp, body=body.strip(), has_frontmatter=True
    )


def dump(doc: NoteDoc) -> str:
    if not doc.has_frontmatter:
        stripped = doc.body.strip()
        return f"{stripped}\n" if stripped else ""
    meta: dict[str, object] = {}
    if doc.author:
        meta["author"] = doc.author
    if doc.timestamp:
        meta["timestamp"] = doc.timestamp
    return fm.dump(meta, doc.body)
