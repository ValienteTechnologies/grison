"""TXT-001 (built-in banned phrases) and TXT-002 (per-workspace confidential terms).

Every banned phrase gets a realistic-hit case AND a realistic-near-miss case that must
NOT fire — the near-miss is what proves the list stays narrow (ordinary pentest prose
about YAML/frontmatter/sync state/merge bases must never trip this)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from grison.validator.terms import BANNED_PHRASES, find_banned_phrases, mask
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_LIB = "findings/library/weak-tls-config.md"
_XSS = "findings/reports/14-acme-corp/reflected-xss.md"


@pytest.mark.rule("TXT-001")
def test_txt001_banned_phrase_fires(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "Disable TLS 1.0/1.1 and weak ciphers; require TLS 1.2 or higher.",
        "As an AI, I recommend disabling TLS 1.0/1.1.",
    )
    assert "TXT-001" in rule_ids(validate_workspace(root))


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_every_banned_phrase_has_a_realistic_hit(phrase: str) -> None:
    text = f"Some prose before. {phrase.capitalize()}, here is the finding detail. More prose."
    hits = find_banned_phrases(text)
    assert any(p == phrase for _line, p in hits), f"{phrase!r} did not fire on its own hit case"


@pytest.mark.parametrize(
    ("phrase", "near_miss"),
    [
        (
            "as an ai",
            "The document was written as an artificial-intelligence-assisted "
            "assessment, not autonomously.",
        ),
        (
            "as an ai language model",
            "The vendor's marketing describes the platform as an AI, language, and "
            "model-agnostic API.",
        ),
        ("i have updated", "The vendor says they have updated the firmware since disclosure."),
        ("i've updated", "The client's ops team says they've updated the WAF rules."),
        ("as requested", "The client's own request tracker flagged this as high priority."),
        (
            "per your instructions",
            "The runbook says: configure the scanner per your organization's instructions.",
        ),
        (
            "let me know if you'd like",
            "The report footer reads: contact us if you would like a re-test.",
        ),
        (
            "i apologize for the confusion",
            "The support ticket read: 'sorry for the confusion caused by the outage.'",
        ),
    ],
)
def test_every_banned_phrase_has_a_realistic_near_miss(phrase: str, near_miss: str) -> None:
    assert phrase in BANNED_PHRASES
    hits = find_banned_phrases(near_miss)
    assert not any(p == phrase for _line, p in hits), (
        f"{phrase!r} falsely fired on near-miss text: {near_miss!r}"
    )


@pytest.mark.rule_ok("TXT-001")
def test_txt001_pentest_vocabulary_never_fires() -> None:
    """The exact words the brief calls out as legitimate (frontmatter/sync
    state/merge base/.grison/) must never be banned — findings legitimately discuss
    YAML frontmatter, and a report about grison itself may discuss sync state."""
    text = (
        "The finding's YAML frontmatter was malformed. The application stores its "
        "session sync state in a client-side cookie signed with a weak merge base "
        "key derived from .grison/ configuration examples found on GitHub."
    )
    assert find_banned_phrases(text) == []


@pytest.mark.rule("TXT-002")
def test_txt002_confidential_term_outside_allowed_prefix(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".grison").mkdir(exist_ok=True)
    (root / ".grison" / "terms.txt").write_text(
        "Acme Corp => findings/reports/14-acme-corp\n"
    )
    edit(
        root / "findings" / "reports" / "globex" / "broken-auth.md",
        "Session tokens increment sequentially",
        "Acme Corp's session tokens increment sequentially",
    )
    fails = validate_workspace(root)
    assert "TXT-002" in rule_ids(fails)
    # the term itself must never be echoed into the failure message
    for f in fails:
        if f.rule_id == "TXT-002":
            assert "Acme Corp" not in f.message
            assert mask("Acme Corp") in f.message


@pytest.mark.rule_ok("TXT-002")
def test_txt002_confidential_term_allowed_inside_its_prefix(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / ".grison").mkdir(exist_ok=True)
    (root / ".grison" / "terms.txt").write_text(
        "Acme Corp => findings/reports/14-acme-corp\n"
    )
    # "Acme Corp" already appears legitimately inside findings/reports/14-acme-corp/
    # (.report.yml, project.md) — must not fire there.
    fails = validate_workspace(root)
    assert "TXT-002" not in rule_ids(fails)


@pytest.mark.rule_ok("TXT-002")
def test_txt002_no_terms_file_is_a_noop(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    assert not (root / ".grison" / "terms.txt").exists()
    assert "TXT-002" not in rule_ids(validate_workspace(root))
