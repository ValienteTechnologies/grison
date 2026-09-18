"""``grison hook post-edit`` (task item 4's PostToolUse hook body)."""

from __future__ import annotations

import json
from pathlib import Path

from grison.remote.bootstrap import bootstrap_workspace
from grison.scaffold.hook import run_post_edit_hook


def _payload(file_path: str, cwd: str) -> str:
    return json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "cwd": cwd,
    })


def test_clean_file_prints_nothing(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    good = tmp_path / "findings" / "library" / "good.md"
    good.write_text(
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    out = run_post_edit_hook(_payload(str(good), str(tmp_path)))
    assert out is None


def test_invalid_file_reports_the_rule_id(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    bad = tmp_path / "findings" / "library" / "bad.md"
    bad.write_text("---\nseverity: not-real\nfinding_type: web\n---\n# x\n")
    out = run_post_edit_hook(_payload(str(bad), str(tmp_path)))
    assert out is not None
    payload = json.loads(out)
    message = payload["hookSpecificOutput"]["systemMessage"]
    assert "FND-003" in message or "FND-009" in message
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_relative_file_path_resolved_against_payload_cwd(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    bad = tmp_path / "findings" / "library" / "bad.md"
    bad.write_text("---\nseverity: not-real\nfinding_type: web\n---\n# x\n")
    out = run_post_edit_hook(_payload("findings/library/bad.md", str(tmp_path)))
    assert out is not None


def test_deleted_file_still_validates_its_containing_directory(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    report_dir = tmp_path / "findings" / "reports" / "acme"
    # not indexed -> IDX-003 would fire for any file under it, but we're checking a
    # deleted path resolves to the parent's cross-file scope rather than erroring.
    ghost = report_dir / "notes" / "gone.md"
    out = run_post_edit_hook(_payload(str(ghost), str(tmp_path)))
    # `notes/` doesn't exist either -> --deleted-ok falls back further; either way this
    # must never raise, and produces feedback or nothing, never a crash.
    assert out is None or "hookSpecificOutput" in json.loads(out)


def test_file_outside_any_workspace_is_silent(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-workspace" / "file.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("hello\n")
    out = run_post_edit_hook(_payload(str(outside), str(tmp_path)))
    assert out is None


def test_file_inside_grison_dir_is_silently_ignored(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    target = tmp_path / ".grison" / "env"
    out = run_post_edit_hook(_payload(str(target), str(tmp_path)))
    assert out is None


def test_garbage_stdin_never_raises() -> None:
    assert run_post_edit_hook("") is None
    assert run_post_edit_hook("not json") is None
    assert run_post_edit_hook("null") is None
    assert run_post_edit_hook("[]") is None
    assert run_post_edit_hook('{"tool_input": "not-a-dict"}') is None
    assert run_post_edit_hook('{"tool_input": {"file_path": 5}}') is None


def test_multiedit_tool_input_shape_is_supported(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    good = tmp_path / "findings" / "library" / "good.md"
    good.write_text(
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": str(good), "edits": [{"old_string": "x", "new_string": "y"}]},
        "cwd": str(tmp_path),
    })
    assert run_post_edit_hook(payload) is None
