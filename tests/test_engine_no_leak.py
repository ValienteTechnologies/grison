"""Keeps ``grison/engine/`` free of any BookStack/Ghostwriter/record-kind knowledge:
grep the package source for the forbidden identifiers, allowing them only inside a
docstring or a ``#`` comment (explaining WHY the engine must stay agnostic of them,
or giving a worked example) — never in real code (an import, a string literal used as
logic, an attribute name)."""

from __future__ import annotations

import re
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1] / "grison" / "engine"

_FORBIDDEN = re.compile(r"bookstack|ghostwriter|\bpage\b|\bfinding\b", re.IGNORECASE)


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Every line NOT inside a triple-quoted docstring and NOT a ``#`` comment,
    1-based line number attached. A small, deliberately simple scanner (this
    codebase's docstrings are always well-formed ``\"\"\"``-delimited blocks) —
    good enough to keep real code honest without a full tokenizer."""
    out: list[tuple[int, str]] = []
    in_doc = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw
        fence_count = line.count('"""')
        stripped = line.strip()
        if in_doc:
            if fence_count % 2 == 1:
                in_doc = False
            continue
        if stripped.startswith('#'):
            continue
        if fence_count >= 1:
            # a docstring opens (and, if fence_count is even, also closes) on this line
            before_fence = line.split('"""', 1)[0]
            if before_fence.strip() and not before_fence.strip().startswith('#'):
                out.append((lineno, before_fence))
            if fence_count % 2 == 1:
                in_doc = True
            continue
        out.append((lineno, line))
    return out


def test_engine_package_names_no_remote_or_record_kind() -> None:
    offenders: list[str] = []
    for path in sorted(ENGINE_DIR.rglob("*.py")):
        for lineno, line in _code_lines(path.read_text(encoding="utf-8")):
            # a trailing "# ..." inline comment is still fine even on a code line
            code_part = line.split("#", 1)[0]
            if _FORBIDDEN.search(code_part):
                offenders.append(f"{path.relative_to(ENGINE_DIR.parent.parent)}:{lineno}: {line!r}")
    assert not offenders, (
        "grison/engine/ must stay record-type-agnostic — found forbidden identifiers "
        "in real code (not a docstring/comment):\n" + "\n".join(offenders)
    )
