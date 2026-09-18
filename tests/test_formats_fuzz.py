"""Property: every ``grison/formats/*`` parser either returns a valid ``Doc`` or
raises :class:`FormatError` (or a ``pydantic.ValidationError``, which every ``parse``
already translates — proven separately in the per-format tests) — NEVER anything else,
for genuinely arbitrary input. This is the brief's "the validator must never raise on
malformed input" guarantee, exercised at the formats layer directly."""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grison.formats import finding, mirrors, narrative, note, wiki
from grison.formats.common import FormatError

_SETTINGS = settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

_text = st.text(max_size=2000)
_bytes_as_text = st.binary(max_size=2000).map(lambda b: b.decode("utf-8", errors="replace"))
_fuzz_text = st.one_of(_text, _bytes_as_text)


@given(text=_fuzz_text)
@_SETTINGS
def test_finding_parse_never_raises_unexpectedly(text: str) -> None:
    for path in (Path("findings/library/x.md"), Path("findings/reports/1-a/x.md")):
        try:
            finding.parse(text, path=path)
        except FormatError:
            pass


@given(text=_fuzz_text)
@_SETTINGS
def test_wiki_parse_never_raises_unexpectedly(text: str) -> None:
    try:
        wiki.parse(text, path=Path("methodology/library/book/page.md"))
    except FormatError:
        pass


@given(text=_fuzz_text)
@_SETTINGS
def test_note_parse_never_raises_unexpectedly(text: str) -> None:
    try:
        note.parse(text, path=Path("findings/reports/1-a/notes/x.md"))
    except FormatError:
        pass


@given(text=_fuzz_text)
@_SETTINGS
def test_narrative_parse_never_raises(text: str) -> None:
    narrative.parse(text, path=Path("findings/reports/1-a/narrative/x.md"))


@given(text=_fuzz_text)
@_SETTINGS
def test_mirror_parsers_never_raise_unexpectedly(text: str) -> None:
    for fn in (
        mirrors.parse_report_meta, mirrors.parse_book_mirror,
        mirrors.parse_chapter_mirror, mirrors.parse_shelf_mirror,
    ):
        try:
            fn(text, path=Path("x"))
        except FormatError:
            pass
    mirrors.parse_project_context(text, path=Path("project.md"))
