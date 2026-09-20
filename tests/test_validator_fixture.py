"""The realistic ``tests/fixtures/ws-v2/`` workspace validates completely clean.

This is the primary passing-case proof for nearly every rule: library findings with
no images, two report directories (one named ``14-acme-corp``, one named plain
``globex`` — proving a report directory's name carries no required id-prefix shape,
WS-004 having been retired) with a top-level embed, a list-item embed, and a
cross-reference, evidence files (PNGs + one .txt), narrative sections, a new local
note AND a mirrored (indexed) note, project.md/.report.yml mirrors, two books with
chapters, a page with code fences full of placeholder tokens, a markdown table, an
internal link, an images/ folder, .grison/manifest.yml + index.json, a
findings/inbox/ with real ``grison parse`` output (including a real
severity/CVSS-band disagreement, proving FND-015 does not apply pre-triage), and a
methodology/checklists/ engagement copy of a library book (proving checklist pages get
full WIKI-…/REF-… validation, with internal links/images resolving) — every rule this
fixture actually exercises the SHAPE of is marked ``rule_ok`` here; a handful of rules
(git hygiene without a repo at all, a hand-edited mirror, banned-text near-misses, a
malformed index.json) need their own dedicated positive case and are marked there
instead (see ``tests/test_validator_ws.py``/``tests/test_validator_txt.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture


@pytest.mark.rule_ok(
    "WS-001",
    "WS-002",
    "WS-003",
    "WS-005",
    "WS-006",
    "WS-007",
    "WS-008",
    "WS-010",
    "FND-001",
    "FND-002",
    "FND-003",
    "FND-004",
    "FND-005",
    "FND-006",
    "FND-007",
    "FND-008",
    "FND-009",
    "FND-010",
    "FND-011",
    "FND-012",
    "FND-013",
    "FND-014",
    "FND-015",
    "FND-016",
    "FND-017",
    "REP-001",
    "REP-002",
    "WIKI-001",
    "WIKI-002",
    "WIKI-003",
    "WIKI-004",
    "WIKI-005",
    "WIKI-006",
    "WIKI-007",
    "WIKI-008",
    "WIKI-009",
    "WIKI-010",
    "WIKI-011",
    "WIKI-012",
    "WIKI-013",
    "WIKI-014",
    "REF-001",
    "REF-002",
    "REF-003",
    "REF-004",
    "REF-005",
    "REF-006",
    "REF-007",
    "REF-008",
    "TXT-001",
    "TXT-002",
    "IDX-001",
    "IDX-002",
    "IDX-003",
    "IDX-004",
)
def test_fixture_validates_clean(tmp_path: Path) -> None:
    # git-init the copy (no commit needed) so the WS-008 git-hygiene check has a real
    # repo to check against — pytest's tmp_path is not itself inside one.
    root = copy_fixture(tmp_path, git=True)
    fails = validate_workspace(root)
    assert fails == [], "\n".join(f"{f.rule_id} {f.path}:{f.line} {f.message}" for f in fails)


def test_fixture_validates_clean_when_narrowed_to_one_file(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path, git=True)
    target = root / "findings" / "reports" / "14-acme-corp" / "reflected-xss.md"
    fails = validate_workspace(root, paths=[target])
    assert fails == []


def test_validate_workspace_never_raises_on_the_fixture_itself() -> None:
    # the committed fixture, not a copy — proves the tracked bytes themselves are valid.
    from tests._ws2_helpers import FIXTURE

    fails = validate_workspace(FIXTURE)
    assert fails == []
