"""``findings/inbox/`` and ``methodology/checklists/`` are local-only (never synced)
but MUST be validated in full — the owner's correction to the original design, which
had exempted them entirely. These tests target the inbox/checklist-specific behaviors
that the general WS-/FND-/WIKI-/IDX- test files don't already cover for those two
trees: inbox tier's own schema exception (the scanner-provenance ``grison:`` block,
and FND-015 not applying pre-triage), and the "never indexed" rule for both trees.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_INBOX_MISMATCH = "findings/inbox/weak-ssh-host-key.md"
_CHECKLIST_PAGE = "methodology/checklists/acme-2026-08/recon.md"


def test_findings_inbox_is_validated_not_skipped(tmp_path: Path) -> None:
    """The correction itself: a broken inbox finding must fail, not pass silently."""
    root = copy_fixture(tmp_path)
    p = root / "findings" / "inbox" / "sql-injection.md"
    edit(p, "severity: high", "severity: not-a-real-severity")
    fails = validate_workspace(root)
    assert "FND-003" in rule_ids(fails)
    assert any(f.path == "findings/inbox/sql-injection.md" for f in fails)


def test_methodology_checklists_is_validated_not_skipped(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "methodology" / "checklists" / "acme-2026-08" / "broken.md").write_text(
        "---\ntitle: ''\n---\nbody\n"
    )
    fails = validate_workspace(root)
    assert "WIKI-002" in rule_ids(fails)


def test_ws002_bad_inbox_content(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "findings" / "inbox" / "sub").mkdir()
    (root / "findings" / "inbox" / "sub" / "x.md").write_text("stray\n")
    fails = validate_workspace(root)
    assert "WS-002" in rule_ids(fails)


def test_ws003_bad_checklist_content(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "methodology" / "checklists" / "acme-2026-08" / "scratch.txt").write_text("stray\n")
    fails = validate_workspace(root)
    assert "WS-003" in rule_ids(fails)


def test_idx_inbox_path_must_never_be_indexed(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    data = json.loads((root / ".grison" / "index.json").read_text(encoding="utf-8"))
    data["records"]["findings/inbox/sql-injection.md"] = {"kind": "gw.finding", "id": 999}
    (root / ".grison" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    fails = validate_workspace(root)
    assert "IDX-002" in rule_ids(fails)
    assert any(f.path == "findings/inbox/sql-injection.md" for f in fails)


def test_idx_checklist_path_must_never_be_indexed(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    data = json.loads((root / ".grison" / "index.json").read_text(encoding="utf-8"))
    data["records"]["methodology/checklists/acme-2026-08/recon.md"] = {
        "kind": "bs.page", "id": 999,
    }
    (root / ".grison" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    fails = validate_workspace(root)
    assert "IDX-002" in rule_ids(fails)
    assert any(f.path == "methodology/checklists/acme-2026-08/recon.md" for f in fails)


def test_ref005_image_in_inbox_finding_has_its_own_message(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / "findings" / "inbox" / "sql-injection.md",
        "affected_entities:",
        "",
    )
    p = root / "findings" / "inbox" / "sql-injection.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace(
        "## Description\n\n", "## Description\n\n![Screenshot](evidence/x.png)\n\n", 1
    ))
    fails = validate_workspace(root)
    matches = [f for f in fails if f.rule_id == "REF-005"]
    assert matches, fails
    assert "inbox" in matches[0].message


def test_grison_block_forbidden_on_library_finding(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    p = root / "findings" / "library" / "weak-tls-config.md"
    edit(p, "severity: medium", "grison:\n  gw:\n    table: reportedFinding\nseverity: medium")
    fails = validate_workspace(root)
    assert "FND-001" in rule_ids(fails)


def test_grison_block_forbidden_on_inbox_finding_too(tmp_path: Path) -> None:
    """No tier carries a ``grison:`` block any more (engine step 3 item E) — the old
    scanner-provenance exception for inbox findings is gone; ``grison parse`` itself
    never writes one, and a hand-added one is a plain unrecognized frontmatter key."""
    root = copy_fixture(tmp_path)
    p = root / "findings" / "inbox" / "sql-injection.md"
    edit(p, "severity: high", "grison:\n  gw:\n    table: reportedFinding\nseverity: high")
    fails = validate_workspace(root)
    assert "FND-001" in rule_ids(fails)
    assert any(f.path == "findings/inbox/sql-injection.md" for f in fails)


@pytest.mark.rule_ok("FND-015")
def test_fnd015_severity_cvss_mismatch_allowed_pre_triage(tmp_path: Path) -> None:
    """The real proof (not just the fixture validating clean incidentally): the SAME
    mismatched content fails once promoted to library tier, and passes as inbox."""
    root = copy_fixture(tmp_path)
    inbox_fails = validate_workspace(root, paths=[root / _INBOX_MISMATCH])
    assert "FND-015" not in rule_ids(inbox_fails)

    text = (root / _INBOX_MISMATCH).read_text(encoding="utf-8")
    # strip the inbox-only grison: block before moving it to instance tier (affected_
    # entities is instance-only, so this must land in a report dir, not library/)
    text = re.sub(r"grison:\n(?:  .*\n)+", "", text, count=1)
    instance_path = root / "findings" / "reports" / "14-acme-corp" / "weak-ssh-host-key.md"
    instance_path.write_text(text, encoding="utf-8")
    instance_fails = validate_workspace(root, paths=[instance_path])
    assert "FND-015" in rule_ids(instance_fails)


def test_checklist_internal_link_resolves_against_library(tmp_path: Path) -> None:
    """'relative to the checklist copy' includes falling back to the original library
    book an inherited link still names (see core.py's _checklist_slugs docstring)."""
    root = copy_fixture(tmp_path)
    fails = validate_workspace(root, paths=[root / _CHECKLIST_PAGE])
    assert "WIKI-007" not in rule_ids(fails)


def test_checklist_broken_internal_link_still_fails(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _CHECKLIST_PAGE,
        "/books/web-application-testing/chapter/reconnaissance",
        "/books/web-application-testing/chapter/does-not-exist",
    )
    fails = validate_workspace(root)
    assert "WIKI-007" in rule_ids(fails)


def test_checklist_mirror_edit_does_not_trigger_ws009(tmp_path: Path) -> None:
    """.book.yml/.chapter.yml in a checklist are copies, not regenerated mirrors —
    WS-009 (hand-edited) must never fire there, even with a recorded digest for the
    ORIGINAL library file of the same name (proves the checklist copy is exempt, not
    merely never-checked because no digest happens to be recorded)."""
    root = copy_fixture(tmp_path)
    lib_book_yml = root / "methodology" / "library" / "web-application-testing" / ".book.yml"
    original = lib_book_yml.read_text(encoding="utf-8")

    from grison.hashing import digest_text
    state_dir = root / ".grison" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "mirrors.json").write_text(json.dumps({
        "methodology/checklists/acme-2026-08/.book.yml": digest_text(original),
    }))

    checklist_book_yml = root / "methodology" / "checklists" / "acme-2026-08" / ".book.yml"
    checklist_book_yml.write_text(original.replace("Web Application Testing", "Edited Copy"))
    fails = validate_workspace(root)
    assert "WS-009" not in rule_ids(fails)
