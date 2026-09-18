"""``CLAUDE.md`` generation (task item 3): deterministic, generated from the live
validator registry + format models, never mentions internal terms, stays short
(no embedded rule table — a `grison validate` failure line already carries its own
rule id, message, and fix; the full rule text lives in `.grison/SPEC.md`).
"""

from __future__ import annotations

from grison.model.enums import FindingType, Severity
from grison.scaffold.claude_md import build_claude_md, marker_line
from grison.scaffold.spec import spec_text
from grison.validator.registry import RULES

_BANNED_INTERNAL_TERMS = ("merge base", "state/", "hash", "index.json", "snapshot")


def test_deterministic() -> None:
    assert build_claude_md() == build_claude_md()


def test_first_line_is_the_marker() -> None:
    text = build_claude_md()
    assert text.splitlines()[0] == marker_line()
    assert "grison scaffold --force" in marker_line()


def test_spec_md_contains_every_active_rule_id() -> None:
    """The full rule table lives in `.grison/SPEC.md` now, not CLAUDE.md — this is
    the "or the model has full context" half; CLAUDE.md just points at it."""
    text = spec_text()
    for rule_id, rule in RULES.items():
        if rule.retired:
            continue
        assert rule_id in text, f"{rule_id} missing from .grison/SPEC.md"


def test_claude_md_does_not_embed_the_rule_table() -> None:
    """CLAUDE.md points at .grison/SPEC.md for rule detail instead of repeating it —
    no active rule id should appear as a literal id in CLAUDE.md's own text."""
    text = build_claude_md()
    for rule_id, rule in RULES.items():
        if rule.retired:
            continue
        assert rule_id not in text, f"{rule_id} should not be embedded in CLAUDE.md"


def test_claude_md_explains_the_failure_line_format_instead() -> None:
    text = build_claude_md()
    assert "RULE-ID" in text
    assert ".grison/SPEC.md" in text


def test_contains_every_severity_and_finding_type_value() -> None:
    text = build_claude_md()
    for member in Severity:
        assert member.value in text
    for member in FindingType:
        assert member.value in text


def test_contains_every_finding_section_name_in_order() -> None:
    from grison.formats.finding import SECTIONS

    text = build_claude_md()
    positions = [text.index(f"## {header}") for header, _ in SECTIONS]
    assert positions == sorted(positions)


def test_never_mentions_internal_terms() -> None:
    text = build_claude_md().lower()
    for term in _BANNED_INTERNAL_TERMS:
        assert term not in text, f"CLAUDE.md leaks internal term {term!r}"


def test_mentions_owner_only_commands_and_forbids_them() -> None:
    text = build_claude_md()
    assert "grison sync" in text
    assert "grison undo" in text
    assert "owner-only" in text


def test_mentions_allowed_commands() -> None:
    text = build_claude_md()
    for cmd in ("grison validate", "grison status", "grison parse"):
        assert cmd in text


def test_stays_under_140_lines() -> None:
    text = build_claude_md()
    assert len(text.splitlines()) < 140


# --- .grison/ (read SPEC.md/templates, never write anywhere in .grison/) -----------


def test_grison_dir_section_names_spec_and_templates_as_readable() -> None:
    text = build_claude_md()
    assert "`.grison/SPEC.md` is the full format spec" in text
    assert "`.grison/templates/` has a starting file for each document type" in text
    assert "never read it, and\nnever write anywhere in `.grison/`" in text


def test_each_document_type_names_its_template() -> None:
    text = build_claude_md()
    assert ".grison/templates/finding-library.md" in text
    assert ".grison/templates/finding-reported.md" in text
    assert ".grison/templates/wiki-page.md" in text
    assert ".grison/templates/project-note.md" in text


# --- payloads that look like template syntax (brief D10) ---------------------------


def test_payloads_section_replaces_the_generic_placeholder_wording() -> None:
    text = build_claude_md()
    assert "Payloads that look like template syntax" in text
    assert "Write it literally, exactly as it was sent" in text
    assert "Do not escape, mangle, or \"defuse\" it" in text
    assert "{7*7}" in text and "{% debug %}" in text
    # the old, wrong-scope wording must be gone
    assert "looks templated" not in text
    assert "Template-looking payloads" not in text


# --- scope discipline must survive ---------------------------------------------------


def test_scope_discipline_section_present() -> None:
    text = build_claude_md()
    assert "## Scope discipline" in text
    assert "EXCLUDED" in text
    assert "project.md" in text
