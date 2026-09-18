"""``.grison/terms.txt`` — the private per-workspace confidential-terms file (brief
D12). Scaffolds an empty, commented template; :mod:`grison.validator.terms` (read
here for the exact syntax it parses) is the sole reader.
"""

from __future__ import annotations

from pathlib import Path

from grison.fsio import atomic_write_text
from grison.validator.terms import TERMS_RELATIVE_PATH

TERMS_TEMPLATE = """\
# grison confidential terms (private — never git-tracked; see docs/workspace-format.md §8.2).
#
# One term per line. '#' starts a comment; blank lines are ignored. Matching is
# case-insensitive, whole-word, Unicode-aware.
#
#   term => allowed/path/prefix
#     allows "term" only under one workspace-relative path prefix (normally one
#     report directory) — e.g.:
#       Acme Corporation => findings/reports/acme-corp
#
#   term
#     (no "=>") is never allowed anywhere in the workspace.
#
# A hit outside its allowed prefix fails `grison validate` (TXT-002), reported only as
# a line number and a masked form (first character, then '*' for the rest) — the term
# itself is never echoed into output that could be committed or copied elsewhere.
"""


def scaffold_terms(root: Path) -> bool:
    """Write ``.grison/terms.txt`` (0600, private) if it doesn't already exist. Never
    overwrites — the file may already hold real, hand-authored confidential terms.
    Returns True if a fresh template was just written."""
    path = root / TERMS_RELATIVE_PATH
    if path.exists():
        return False
    atomic_write_text(path, TERMS_TEMPLATE, private=True)
    return True
