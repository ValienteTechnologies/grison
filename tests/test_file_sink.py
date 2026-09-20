"""Tests for the file sink — stem disambiguation and per-finding write isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.formats.finding import FindingDoc
from grison.sinks import file_sink
from grison.sinks.file_sink import FileSink


def _finding(*, title: str = "T", desc: str = "body") -> FindingDoc:
    data = {
        "severity": "medium",
        "finding_type": "web",
        "title": title,
        "description": desc,
    }
    return FindingDoc.model_validate(data, context={"tier": "inbox"})


def test_same_slug_and_same_key_still_produce_distinct_files(tmp_path: Path) -> None:
    # e.g. the same Nessus plugin firing in two export files parsed in one run:
    # identical title (slug) and identical dedupe key, but different content.
    a = _finding(title="Weak TLS Cipher", desc="host A")
    b = _finding(title="Weak TLS Cipher", desc="host B")
    result = FileSink(tmp_path).write([a, b], keys=["19506", "19506"])

    assert len(result.written) == 2
    assert len(set(result.written)) == 2  # no duplicate path

    md_files = sorted(tmp_path.glob("*.md"))
    assert len(md_files) == 2
    contents = {p.read_text(encoding="utf-8") for p in md_files}
    assert file_sink.finding_to_markdown(a) in contents
    assert file_sink.finding_to_markdown(b) in contents
    for content in contents:
        assert "grison:" not in content


def test_write_isolates_a_failing_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good1 = _finding(title="Good One")
    boom = _finding(title="Boom")
    good2 = _finding(title="Good Two")
    real = file_sink.finding_to_markdown

    def flaky(f: FindingDoc) -> str:
        if f.title == "Boom":
            raise ValueError("kaboom")
        return real(f)

    monkeypatch.setattr(file_sink, "finding_to_markdown", flaky)
    result = FileSink(tmp_path).write([good1, boom, good2])

    assert len(result.written) == 2
    assert len(result.errors) == 1
    assert "kaboom" in result.errors[0]
    # the other findings still landed on disk despite the failure
    assert sorted(p.name for p in tmp_path.glob("*.md")) == sorted(p.name for p in result.written)
