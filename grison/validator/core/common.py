"""Shared low-level helpers used across the ``grison.validator.core`` package: path
formatting, text reading, ``FormatError`` -> rule-id mapping, the banned/confidential
text scan, and the sync-mirror digest check.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from grison.formats.common import FormatError
from grison.hashing import digest_text
from grison.validator import registry
from grison.validator import terms as terms_mod
from grison.validator.mirrors import expected_digest
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm

# (kind FormatError -> rule id) per document family. A kind not present here falls
# back to the family's own catch-all rule.
_FND_KIND_TO_RULE: dict[str, str] = {
    "unknown_field": registry.FND_UNKNOWN_FIELD,
    "missing_field": registry.FND_MISSING_FIELD,
    "bad_severity": registry.FND_BAD_SEVERITY,
    "bad_finding_type": registry.FND_BAD_FINDING_TYPE,
    "bad_cvss": registry.FND_BAD_CVSS,
    "bad_cwe": registry.FND_BAD_CWE,
    "bad_tags": registry.FND_BAD_TAGS,
    "affected_entities_on_library": registry.FND_AFFECTED_ENTITIES_ON_LIBRARY,
    "missing_section": registry.FND_MISSING_SECTION,
    "unknown_section": registry.FND_UNKNOWN_SECTION,
    "duplicate_section": registry.FND_DUPLICATE_SECTION,
    "sections_out_of_order": registry.FND_SECTIONS_OUT_OF_ORDER,
    "missing_title": registry.FND_BAD_TITLE,
    "empty_title": registry.FND_BAD_TITLE,
    "unexpected_content": registry.FND_UNEXPECTED_CONTENT,
}
_WIKI_KIND_TO_RULE: dict[str, str] = {
    "unknown_field": registry.WIKI_UNKNOWN_FIELD,
    "missing_field": registry.WIKI_BAD_TITLE,
    "bad_title": registry.WIKI_BAD_TITLE,
    "bad_priority": registry.WIKI_BAD_PRIORITY,
    "bad_tags": registry.WIKI_BAD_TAGS,
}


def _fmt_failure(rel: str, e: FormatError, kind_map: dict[str, str], default_rule: str) -> Failure:
    rule_id = kind_map.get(e.kind, default_rule)
    return fail(rule_id, rel, e.detail or e.kind, line=e.line)


def _read_text(root: Path, rel: str, *, default_rule: str) -> tuple[str | None, Failure | None]:
    try:
        # newline="" disables Python's universal-newline translation — a real CRLF
        # byte sequence on disk must reach WIKI-009's own check as "\r\n", not get
        # silently rewritten to "\n" by the read itself.
        with (root / rel).open(encoding="utf-8", newline="") as fh:
            return fh.read(), None
    except (OSError, UnicodeDecodeError) as e:
        return None, fail(default_rule, rel, f"cannot read file: {e}")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_txt(rel: str, text: str, cterms: list[ConfidentialTerm]) -> list[Failure]:
    out: list[Failure] = []
    for lineno, phrase in terms_mod.find_banned_phrases(text):
        out.append(fail(registry.TXT_BANNED_PHRASE, rel, f"banned phrase: {phrase!r}", line=lineno))
    for term in cterms:
        if terms_mod.path_allows(rel, term.allowed_prefix):
            continue
        for lineno in terms_mod.find_term(text, term.term):
            out.append(
                fail(
                    registry.TXT_CONFIDENTIAL_TERM,
                    rel,
                    f"confidential term: {terms_mod.mask(term.term)}",
                    line=lineno,
                )
            )
    return out


@dataclass
class _EmbedHit:
    path: str  # relative to workspace root
    ref: str  # the "evidence/x.png" or "images/x.png" spelling
    caption: str
    doc_rel: str
    line: int


def _check_mirror(root: Path, rel: str, text: str) -> list[Failure]:
    digest = expected_digest(root, rel)
    if digest is None:
        return []
    if digest_text(text) != digest:
        return [fail(registry.WS_MIRROR_EDITED, rel, "content differs from the last sync")]
    return []
