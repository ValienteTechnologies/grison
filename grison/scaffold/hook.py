"""``grison hook post-edit`` — the ``PostToolUse`` hook body scaffolded into
``.claude/settings.json`` (brief D11). A pure function (:func:`run_post_edit_hook`)
plus a thin ``main()`` the CLI command calls, so the whole contract is testable in
Python with no subprocess/shell involved — see
``grison/scaffold/settings_json.py``'s module docstring for the stdin/stdout contract
and exit-code semantics this implements.

Contract: read the ``PostToolUse`` JSON payload from stdin, validate ONLY the edited
file (``tool_input.file_path``, scoped with ``deleted_ok=True`` since the hook also
runs after a delete), and — only when there are failures — print a
``hookSpecificOutput.systemMessage`` JSON object so Claude sees them. Silent (no
stdout) when the file validates clean, or sits outside any grison workspace, or
outside that workspace's validated trees (``findings/``/``methodology/``) entirely.
Never raises, never exits nonzero — a ``PostToolUse`` hook cannot block the tool call
anyway (it already ran); this only ever informs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from grison.validator import (
    ValidationScopeError,
    WorkspaceNotFound,
    find_workspace_root,
    validate_workspace,
)
from grison.validator.registry import Failure


def _extract_file_path(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) and file_path else None


def _extract_cwd(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _resolve_path(file_path: str, cwd: str | None) -> Path:
    p = Path(file_path)
    base = Path(cwd) if cwd else Path.cwd()
    return (p if p.is_absolute() else base / p).resolve()


def _render_feedback(failures: list[Failure]) -> str:
    lines = []
    for f in failures:
        loc = f"{f.path}:{f.line}" if f.line is not None else f.path
        lines.append(f"{loc}: {f.rule_id} {f.message} — {f.fix}")
    return f"grison validate found {len(failures)} issue(s):\n" + "\n".join(lines)


def run_post_edit_hook(stdin_text: str) -> str | None:
    """The stdout text to print (a JSON object), or ``None`` to print nothing —
    never raises: every unrecognized/out-of-scope input is treated as "nothing to
    report", exactly like the settings.json docstring's "ignore files outside the
    workspace's validated trees silently" requirement."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else None
    except ValueError:
        return None

    file_path = _extract_file_path(payload)
    if file_path is None:
        return None
    target = _resolve_path(file_path, _extract_cwd(payload))

    try:
        root = find_workspace_root(target.parent)
    except WorkspaceNotFound:
        return None

    try:
        failures = validate_workspace(root, paths=[target], deleted_ok=True)
    except ValidationScopeError:
        return None
    except Exception:  # noqa: BLE001 — a hook must never crash the agent's turn
        return None

    if not failures:
        return None
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "systemMessage": _render_feedback(failures),
        }
    })


def main() -> int:
    """``grison hook post-edit`` — always returns 0 (see the module docstring)."""
    try:
        output = run_post_edit_hook(sys.stdin.read())
        if output:
            print(output)
    except Exception:  # noqa: BLE001 — never block/crash the agent's turn
        pass
    return 0
