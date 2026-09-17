"""D12 banned text: a small built-in phrase list (process narration / assistant
chatter / tool-internals talk) plus per-workspace confidential terms.

``BANNED_PHRASES`` stays deliberately narrow — pentest report prose legitimately talks
about "frontmatter", "sync state", YAML, merge bases, etc., so nothing generic is on
this list; every entry here has a realistic-hit test AND a realistic-near-miss test in
``tests/test_validator_txt.py`` (see that file for exactly why each near-miss must NOT
fire).

Confidential terms live in the PRIVATE file ``.grison/terms.txt`` (one term per line,
``#`` starts a comment, blank lines ignored; ``term => allowed/path/prefix`` allows a
term only under one workspace-relative path prefix — usually one report directory;
a bare ``term`` line with no ``=>`` is never allowed anywhere). Matching is
case-insensitive, whole-word, Unicode-aware. A hit is reported WITHOUT ever echoing the
term itself (the term list is private and must never appear in output that could be
committed/copied) — only the line number and a masked form (first character, then
``*`` for the rest).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TERMS_RELATIVE_PATH = ".grison/terms.txt"

# Every phrase below reads as the ASSISTANT talking about ITS OWN process/tooling
# inside a document body an engagement reader would see — not ordinary security
# prose. Matched case-insensitively with internal whitespace collapsing to any run of
# whitespace (so "as an AI" / "as an  AI" both hit), word-bounded on each end.
BANNED_PHRASES: tuple[str, ...] = (
    "as an ai",
    "as an ai language model",
    "i have updated",
    "i've updated",
    "as requested",
    "per your instructions",
    "let me know if you'd like",
    "i apologize for the confusion",
)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


_BANNED_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (p, _phrase_re(p)) for p in BANNED_PHRASES
)


def find_banned_phrases(text: str) -> list[tuple[int, str]]:
    """``[(1-based line number, phrase)]`` for every built-in banned-phrase hit."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for phrase, rx in _BANNED_RES:
            if rx.search(line):
                hits.append((lineno, phrase))
    return hits


@dataclass(frozen=True)
class ConfidentialTerm:
    term: str
    allowed_prefix: str | None  # None = never allowed anywhere in the workspace


def load_terms(root: Path) -> list[ConfidentialTerm]:
    """Parse ``.grison/terms.txt``. Missing file -> no terms (nothing to check)."""
    path = root / TERMS_RELATIVE_PATH
    if not path.exists():
        return []
    terms: list[ConfidentialTerm] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=>" in line:
            term, _, prefix = line.partition("=>")
            term, prefix = term.strip(), prefix.strip()
            if term:
                terms.append(ConfidentialTerm(term, prefix or None))
        else:
            terms.append(ConfidentialTerm(line, None))
    return terms


def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE | re.UNICODE)


def find_term(text: str, term: str) -> list[int]:
    """1-based line numbers where ``term`` appears (whole-word, case-insensitive)."""
    rx = _term_re(term)
    return [i for i, line in enumerate(text.splitlines(), start=1) if rx.search(line)]


def path_allows(rel_path: str, allowed_prefix: str | None) -> bool:
    """True if ``allowed_prefix`` covers ``rel_path`` (equal, or a real path-segment
    prefix of it — ``"a/b"`` covers ``"a/b/c.md"`` but not ``"a/bc.md"``)."""
    if allowed_prefix is None:
        return False
    prefix = allowed_prefix.rstrip("/")
    return rel_path == prefix or rel_path.startswith(prefix + "/")


def mask(term: str) -> str:
    """First character, then ``*`` for the rest — never the term itself."""
    if len(term) <= 1:
        return "*"
    return term[0] + "*" * (len(term) - 1)
