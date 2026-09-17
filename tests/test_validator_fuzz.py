"""Property: ``validate_workspace`` never raises, no matter how a real workspace file
is corrupted — every file type gets random bytes/text substituted in, one at a time,
against a real (copied) workspace, and the call must always return a list."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture

_SETTINGS = settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])

_text = st.text(max_size=1500)
_bytes_as_text = st.binary(max_size=1500).map(lambda b: b.decode("utf-8", errors="replace"))
_fuzz_text = st.one_of(_text, _bytes_as_text)

_TARGET_FILES = (
    "findings/library/weak-tls-config.md",
    "findings/reports/14-acme-corp/reflected-xss.md",
    "findings/reports/14-acme-corp/narrative/executive_summary.md",
    "findings/reports/14-acme-corp/notes/follow-up.md",
    "findings/reports/14-acme-corp/notes/8-client-note.md",
    "findings/reports/14-acme-corp/.report.yml",
    "findings/reports/14-acme-corp/project.md",
    "methodology/library/web-application-testing/recon.md",
    "methodology/library/web-application-testing/.book.yml",
    "methodology/library/web-application-testing/reconnaissance/.chapter.yml",
    "methodology/library/.shelves/pentest-methodologies.yml",
    ".grison/manifest.yml",
    ".grison/index.json",
)


@given(target=st.sampled_from(_TARGET_FILES), text=_fuzz_text)
@_SETTINGS
def test_validate_workspace_never_raises_on_a_corrupted_file(target: str, text: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = copy_fixture(Path(td))
        (root / target).write_text(text, encoding="utf-8")
        result = validate_workspace(root)
        assert isinstance(result, list)


@given(text=_fuzz_text)
@_SETTINGS
def test_validate_workspace_never_raises_on_corrupted_terms_file(text: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = copy_fixture(Path(td))
        (root / ".grison").mkdir(exist_ok=True)
        (root / ".grison" / "terms.txt").write_text(text, encoding="utf-8")
        result = validate_workspace(root)
        assert isinstance(result, list)
