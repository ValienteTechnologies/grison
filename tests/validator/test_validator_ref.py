"""Failing-case tests for REF-001..REF-010 (D1 evidence / D9 wiki-image references)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grison.validator import validate_workspace
from tests._ws2_helpers import copy_fixture, edit, rule_ids

_XSS = "findings/reports/14-acme-corp/reflected-xss.md"
_LIB = "findings/library/weak-tls-config.md"
_RECON = "methodology/library/web-application-testing/recon.md"
_SUBDOMAIN = "methodology/library/web-application-testing/reconnaissance/subdomain-enum.md"


@pytest.mark.rule("REF-001")
def test_ref001_image_not_alone(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _XSS,
        '![Alert firing in the browser](evidence/xss-alert.png "captured during testing")',
        'See ![Alert firing in the browser](evidence/xss-alert.png "captured during testing") '
        "above.",
    )
    assert "REF-001" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-002")
def test_ref002_unresolved_image(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(root / _XSS, 'evidence/xss-alert.png "captured', 'evidence/missing.png "captured')
    assert "REF-002" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-003")
def test_ref003_stem_collision(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "xss-alert.txt").write_text("same stem as xss-alert.png\n")
    assert "REF-003" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-004")
def test_ref004_caption_conflict(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    narrative = (
        root / "findings" / "reports" / "14-acme-corp" / "narrative" / "executive_summary.md"
    )
    edit(
        narrative,
        "One critical and one high finding",
        "![A completely different caption](evidence/xss-alert.png)\n\n"
        "One critical and one high finding",
    )
    fails = validate_workspace(root)
    assert "REF-004" in rule_ids(fails)


@pytest.mark.rule("REF-005")
def test_ref005_image_in_library(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _LIB,
        "Traffic may be susceptible to downgrade and cryptographic attacks.",
        "Traffic may be susceptible.\n\n![Not allowed](evidence/anything.png)",
    )
    assert "REF-005" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-006")
def test_ref006_bad_cross_reference(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _XSS,
        "[the alert screenshot](evidence/xss-alert.png)",
        "[the alert screenshot](evidence/missing.png)",
    )
    assert "REF-006" in rule_ids(validate_workspace(root))


@pytest.mark.rule("REF-007")
def test_ref007_wrong_wiki_image_spelling(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    edit(
        root / _SUBDOMAIN,
        "../images/recon-diagram.png",
        "images/recon-diagram.png",
    )
    assert "REF-007" in rule_ids(validate_workspace(root))


def test_evidence_collision_sidecar_never_counts_toward_ref003(tmp_path: Path) -> None:
    """Item 5 (fix-d): a live collision sidecar (``<name>.remote.<ext>``,
    ENGINE.md §8) is never a real evidence file — the validator's evidence-
    directory scan must exclude it, the same way ``grison.engine.filesets``'s own
    local scan and ``grison status``'s sidecar-aware counts already do.
    Demonstrated with an extension-less shadowed file (``readme`` -> sidecar
    ``readme.remote``): without the exclusion,
    ``PurePosixPath("readme.remote").stem == "readme"`` collides with the real
    file's own stem — a false REF-003 that could never fire for any REAL pair of
    evidence files (every real one has an extension — see
    ``grison.engine.filesets``'s module docstring)."""
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "readme").write_text("not really an image, just proving the exclusion\n")
    (ev / "readme.remote").write_text("a stale collision sidecar, never a real file\n")
    assert "REF-003" not in rule_ids(validate_workspace(root))


def test_non_ascii_evidence_filename_validates(tmp_path: Path) -> None:
    """markdown-it-py percent-encodes non-ASCII bytes in an image destination
    (``evidence/Phishing_Sonu%C3%A7lar%C4%B1.png`` for the author's own
    ``evidence/Phishing_Sonuçları.png``) — without decoding it back before
    resolving against the filesystem, this failed REF-002 even though the file
    exists at exactly that path. Also proves item 2 (engine-findings-lab.md
    Scenario 6): this exact name — non-ASCII AND uppercase — used to ALSO fail
    ``WS-001`` (its charset rule doesn't allow either), even though grison
    itself keeps evidence names verbatim and never renames one; ``WS-001`` no
    longer applies inside ``evidence/`` at all (``REF-008`` — see
    ``test_ref008_*`` — replaces it there)."""
    root = copy_fixture(tmp_path)
    evidence_dir = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (evidence_dir / "Phishing_Sonuçları.png").write_bytes(b"\x89PNG-fake-bytes")
    narrative = (
        root / "findings" / "reports" / "14-acme-corp" / "narrative" / "executive_summary.md"
    )
    edit(
        narrative,
        "One critical and one high finding were identified",
        "![Phishing results](evidence/Phishing_Sonuçları.png)\n\n"
        "One critical and one high finding were identified",
    )
    fails = rule_ids(validate_workspace(root))
    assert "REF-002" not in fails
    assert "REF-006" not in fails
    assert "WS-001" not in fails
    assert "REF-008" not in fails


@pytest.mark.rule("REF-008")
def test_ref008_bad_fileset_names(tmp_path: Path) -> None:
    """Item 2 (engine-findings-lab.md 'Scenario 6'): ``WS-001``'s charset rule
    does not apply inside ``evidence/`` or ``images/`` (D1/D9: names are stable
    handles kept verbatim) — ``REF-008`` replaces it there, rejecting a leading
    dot and a collision-sidecar shape (never a real file) regardless of the
    name otherwise being fine on every other axis (both names below are valid
    non-ASCII/mixed-case names apart from the one thing that's wrong)."""
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / ".Şifre.png").write_bytes(b"\x89PNG")
    (ev / "Şifre.remote.png").write_bytes(b"\x89PNG")
    img = root / "methodology" / "library" / "network-testing" / "images"
    (img / ".Hidden.png").write_bytes(b"\x89PNG")

    fails = validate_workspace(root)
    ref008_paths = {f.path for f in fails if f.rule_id == "REF-008"}
    assert "findings/reports/14-acme-corp/evidence/.Şifre.png" in ref008_paths
    assert "findings/reports/14-acme-corp/evidence/Şifre.remote.png" in ref008_paths
    assert "methodology/library/network-testing/images/.Hidden.png" in ref008_paths

    # a path separator can never appear inside one real filesystem entry's own
    # name (the two real callers only ever hand this a directory-scan result),
    # so it's checked directly against the unit-level rule instead.
    from pathlib import PurePosixPath

    from grison.validator.core import _check_fileset_name

    sep_fails = _check_fileset_name(PurePosixPath("findings/reports/r/evidence"), "sub/dir.png")
    assert len(sep_fails) == 1 and sep_fails[0].rule_id == "REF-008"


@pytest.mark.rule_ok("REF-008")
def test_ref008_non_ascii_and_uppercase_names_are_fine(tmp_path: Path) -> None:
    """The exact motivating case (Scenario 6, D1's own real-data example): a
    non-ASCII, mixed-case evidence/image name is a stable handle kept
    verbatim — ``REF-008`` has no charset opinion at all, only ``WS-001`` did,
    and ``WS-001`` no longer applies inside ``evidence/``/``images/``."""
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "Sonuçları_ş.png").write_bytes(b"\x89PNG")
    img = root / "methodology" / "library" / "network-testing" / "images"
    (img / "Screenshot ÜPPER.PNG").write_bytes(b"\x89PNG")

    fails = validate_workspace(root)
    assert "WS-001" not in rule_ids(fails)
    assert "REF-008" not in rule_ids(fails)


@pytest.mark.rule("REF-009")
def test_ref009_bad_evidence_extension(tmp_path: Path) -> None:
    """Real-workspace defect (2026-09-22): Ghostwriter's own
    ``EVIDENCE_ALLOWED_EXTENSIONS`` (``txt``, ``md``, ``log``, ``jpg``, ``jpeg``,
    ``png``) rejects anything else server-side — this must fail offline first."""
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "capture.gif").write_bytes(b"GIF89a")
    (ev / "notes.html").write_bytes(b"<html></html>")

    fails = validate_workspace(root)
    ref009 = {f.path: f.message for f in fails if f.rule_id == "REF-009"}
    assert "findings/reports/14-acme-corp/evidence/capture.gif" in ref009
    assert ".png" in ref009["findings/reports/14-acme-corp/evidence/capture.gif"]
    assert "findings/reports/14-acme-corp/evidence/notes.html" in ref009
    assert ".txt" in ref009["findings/reports/14-acme-corp/evidence/notes.html"]


@pytest.mark.rule_ok("REF-009")
def test_ref009_case_insensitive_and_images_exempt(tmp_path: Path) -> None:
    """The extension check is case-insensitive (``.PNG`` is fine), and never applies
    to a wiki ``images/`` file at all — Ghostwriter's evidence-extension allow-list
    has nothing to do with BookStack."""
    root = copy_fixture(tmp_path)
    ev = root / "findings" / "reports" / "14-acme-corp" / "evidence"
    (ev / "Shout.PNG").write_bytes(b"\x89PNG")
    img = root / "methodology" / "library" / "network-testing" / "images"
    (img / "diagram.gif").write_bytes(b"GIF89a")

    fails = validate_workspace(root)
    assert "REF-009" not in rule_ids(fails)


@pytest.mark.rule("REF-010")
def test_ref010_caption_too_long(tmp_path: Path) -> None:
    """Real-workspace defect (2026-09-22): Ghostwriter's ``Evidence.caption`` is a
    255-char ``CharField`` — an over-long embed caption must fail offline first."""
    root = copy_fixture(tmp_path)
    narrative = (
        root / "findings" / "reports" / "14-acme-corp" / "narrative" / "executive_summary.md"
    )
    long_caption = "A" * 300
    edit(
        narrative,
        "One critical and one high finding",
        f"![{long_caption}](evidence/raw-request.txt)\n\nOne critical and one high finding",
    )
    fails = validate_workspace(root)
    ref010 = [f for f in fails if f.rule_id == "REF-010"]
    assert len(ref010) == 1
    assert "300" in ref010[0].message
    assert "255" in ref010[0].message


def test_ref010_caption_too_long_in_a_note(tmp_path: Path) -> None:
    """Review finding: a note's embeds were checked for position/resolution
    (``_check_narrative_body``) but never joined the report-wide caption scan, so
    a note's own over-long caption passed ``grison validate`` — only findings and
    narrative sections were caught. ``notes/follow-up.md`` is a real, unindexed
    (frontmatter-less) note in the fixture; ``raw-request.txt`` is a real,
    otherwise-unreferenced evidence file, so this exercises nothing but REF-010."""
    root = copy_fixture(tmp_path)
    note = root / "findings" / "reports" / "14-acme-corp" / "notes" / "follow-up.md"
    long_caption = "A" * 300
    edit(
        note,
        "Ask the client whether the staging environment is in scope too.",
        f"![{long_caption}](evidence/raw-request.txt)\n\n"
        "Ask the client whether the staging environment is in scope too.",
    )
    fails = validate_workspace(root)
    ref010 = [f for f in fails if f.rule_id == "REF-010"]
    assert len(ref010) == 1
    assert ref010[0].path == "findings/reports/14-acme-corp/notes/follow-up.md"
    assert "300" in ref010[0].message
    assert "255" in ref010[0].message
