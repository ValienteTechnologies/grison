"""Workspace-root ``.gitignore`` — merged idempotently, like ``.claude/settings.json``.

This is NOT ``.grison/.gitignore`` (the private allow-list :func:`grison.manifest.write_gitignore`
already owns — see that module's docstring) — this is the workspace ROOT's own
``.gitignore``, which must never blanket-ignore ``.grison/`` (the v1 scaffold did; the
migration rewrites that line — see ``docs/workspace-format.md`` §1.7/§1.9). The one
thing grison scaffolds here is ``*.remote.*``, the collision-sidecar pattern
(ENGINE.md's "Collision sidecars": ``<name>.remote.md``/``.remote.<ext>`` files the
sync engine writes next to a colliding record — never documents, never committed).
"""

from __future__ import annotations

from pathlib import Path

from grison.fsio import atomic_write_text

GITIGNORE_RELATIVE_PATH = ".gitignore"

_BEGIN = "# --- grison-managed (grison scaffold) — do not hand-edit this block ---"
_END = "# --- end grison-managed ---"
GRISON_GITIGNORE_ENTRIES: tuple[str, ...] = ("*.remote.*",)


def _strip_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_block = False
    for line in lines:
        if line.strip() == _BEGIN:
            in_block = True
            continue
        if line.strip() == _END:
            in_block = False
            continue
        if not in_block:
            out.append(line)
    return out


def _has_block(lines: list[str]) -> bool:
    return any(line.strip() == _BEGIN for line in lines)


def build_gitignore(existing: str | None, *, force: bool = False) -> str:
    """Merge grison's block into ``existing`` (or start fresh). Like
    ``settings_json.build_settings``: a plain run self-heals the block back in if it's
    missing (this guards a real enforcement path — collision sidecars must never be
    committed); ``force`` additionally strips and rewrites the block from scratch
    (handles a future wording/entry change cleanly instead of ending up with both)."""
    lines = (existing or "").splitlines()
    if force:
        lines = _strip_block(lines)
    if _has_block(lines):
        text = "\n".join(lines)
    else:
        block = [_BEGIN, *GRISON_GITIGNORE_ENTRIES, _END]
        if lines and lines[-1].strip():
            lines = [*lines, ""]
        text = "\n".join([*lines, *block])
    return text.rstrip("\n") + "\n"


def ensure_gitignore(root: Path, *, force: bool = False) -> None:
    path = root / GITIGNORE_RELATIVE_PATH
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    atomic_write_text(path, build_gitignore(existing, force=force))
