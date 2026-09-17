"""Proofs for the event-identity enforcement (coordinator feedback item 2) and the
outcome->verb mapping."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from grison.engine import events
from grison.engine.model import Event, Outcome, Plan, RemoteRecord, Veto, VetoSeverity


class _FakeAdapter:
    kind = "gw.finding"

    def remote_label(self, data: object) -> str:
        assert isinstance(data, dict)
        return f'"{data["name"]}" (page {data["id"]})'


def test_event_without_path_or_label_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="neither a path nor a remote label"):
        Event(verb="skip", path=None, label=None)


def test_event_with_only_a_label_is_fine() -> None:
    e = Event(verb="skip", path=None, label='"X" (page 1)')
    assert e.subject == '"X" (page 1)'


def test_event_with_only_a_path_is_fine() -> None:
    e = Event(verb="push", path="a/b.md")
    assert e.subject == "a/b.md"


def test_build_event_uses_path_when_present() -> None:
    plan = Plan(kind="gw.finding", outcome=Outcome.PUSH, path=PurePosixPath("a/b.md"))
    e = events.build_event("push", plan, _FakeAdapter())
    assert e.path == "a/b.md"
    assert e.label is None
    assert e.subject == "a/b.md"


def test_build_event_falls_back_to_remote_label_when_path_is_none() -> None:
    remote = RemoteRecord(id=28, data={"id": 28, "name": "WYSIWYG Created Page"})
    plan = Plan(kind="bs.page", outcome=Outcome.SKIP, path=None, remote=remote,
               severity=VetoSeverity.INFO)
    e = events.build_event("skip", plan, _FakeAdapter(), detail="not markdown-native")
    assert e.path is None
    assert e.label == '"WYSIWYG Created Page" (page 28)'
    assert e.subject == '"WYSIWYG Created Page" (page 28)'
    assert e.severity is VetoSeverity.INFO


def test_build_event_falls_back_to_kind_and_id_with_no_remote_data_either() -> None:
    plan = Plan(kind="bs.page", outcome=Outcome.FAILED, path=None, id=42)
    e = events.build_event("failed", plan, _FakeAdapter())
    assert e.label == "bs.page 42"


def test_render_text_hides_info_severity_by_default() -> None:
    info = Event(verb="skip", path=None, label='"Draft" (page 1)', severity=VetoSeverity.INFO)
    attention = Event(verb="skip", path="a.md", severity=VetoSeverity.ATTENTION)
    lines = events.render_text_lines([info, attention])
    assert lines == ["skip a.md"]
    verbose_lines = events.render_text_lines([info, attention], verbose=True)
    assert len(verbose_lines) == 2


def test_render_json_always_includes_info_severity() -> None:
    info = Event(verb="skip", path=None, label='"Draft" (page 1)', severity=VetoSeverity.INFO)
    out = events.render_json([info])
    assert '"severity": "info"' in out


def test_veto_default_severity_is_attention() -> None:
    assert Veto("reason").severity is VetoSeverity.ATTENTION
