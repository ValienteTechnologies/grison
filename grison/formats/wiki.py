"""Format v2 wiki page documents: ``methodology/library/<book>/[<chapter>/]*.md``.

D4: a page's book and chapter come from its directory only — ``book``/``chapter``
frontmatter keys are gone. Frontmatter carries exactly ``title``, ``priority``, ``tags``
— nothing else (D3: no ``grison:`` block, no page id; the id lives in
``.grison/index.json``). The body is markdown BookStack takes verbatim (no converter —
BookStack pages are markdown-native); its hygiene (raw HTML, link schemes, internal-link
resolution, image form, control characters, heading structure) is a
:mod:`grison.validator` concern, not this module's — this module only owns the
frontmatter/body split and the three frontmatter fields' own shape.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from grison.formats.common import FormatError, translate_validation_error, validate_tags
from grison.markdown import frontmatter as fm
from grison.markdown.frontmatter import DocumentError


class WikiPageDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    priority: int | None = None
    tags: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, raw: list[str]) -> list[str]:
        return validate_tags(raw)


def parse(text: str, *, path: Path) -> WikiPageDoc:
    del path
    try:
        meta, body = fm.split(text)
    except DocumentError as e:
        raise FormatError("bad_frontmatter", str(e)) from e
    data: dict[str, object] = dict(meta)
    data["body"] = body.strip()
    try:
        return WikiPageDoc.model_validate(data)
    except ValidationError as e:
        raise translate_validation_error(e, text=text) from e


def dump(doc: WikiPageDoc) -> str:
    meta: dict[str, object] = {"title": doc.title}
    if doc.priority is not None:
        meta["priority"] = doc.priority
    if doc.tags:
        meta["tags"] = doc.tags
    return fm.dump(meta, doc.body)
