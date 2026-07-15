"""The grison workspace — the git-like markdown tree that mirrors the remotes.

The tree *is* the 2×2 data model: ``findings/`` ⇄ Ghostwriter, ``methodology/`` ⇄
BookStack, with a durable ``library/`` and per-engagement tiers under each. Any
verb in a fresh dir scaffolds what it needs (the binary bootstraps — there is no
``init``).

``findings/inbox/`` (parse output) and ``methodology/checklists/`` (per-engagement
working copies) are **local-only by construction** — sync only ever scans
``findings/library``, ``findings/reports`` and ``methodology/library``. An engagement
checklist is just ``cp -r methodology/library/<book> methodology/checklists/<engagement>``:
the 2×2's fourth cell needs no remote and no new verb.
"""

from __future__ import annotations

from pathlib import Path

# Relative dirs that make up the workspace tree. Only findings/library, findings/
# reports and methodology/library ever sync; inbox and checklists are local-only.
WORKSPACE_DIRS: tuple[str, ...] = (
    "findings/inbox",
    "findings/library",
    "findings/reports",
    "methodology/library",
    "methodology/checklists",
)


def bootstrap_tree(root: Path) -> list[Path]:
    """Create any missing workspace dirs under ``root``; return the ones created."""
    created: list[Path] = []
    for rel in WORKSPACE_DIRS:
        d = root / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return created


def inbox_dir(root: Path) -> Path:
    """Where ``parse`` writes proto-instances for triage (local-only, never synced)."""
    return root / "findings" / "inbox"
