"""``.grison/terms.txt`` scaffold (task item 8, brief D12)."""

from __future__ import annotations

import stat
from pathlib import Path

from grison.scaffold import terms as terms_mod
from grison.validator.terms import load_terms


def test_scaffolds_private_template(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    created = terms_mod.scaffold_terms(tmp_path)
    assert created is True
    path = tmp_path / terms_mod.TERMS_RELATIVE_PATH
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_template_has_no_active_terms(tmp_path: Path) -> None:
    """The scaffolded template is all comments — nothing should ever be parsed as a
    real (accidentally-enforced) confidential term out of the box."""
    (tmp_path / ".grison").mkdir()
    terms_mod.scaffold_terms(tmp_path)
    assert load_terms(tmp_path) == []


def test_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / ".grison").mkdir()
    path = tmp_path / terms_mod.TERMS_RELATIVE_PATH
    path.write_text("real-secret-codename\n")
    path.chmod(0o600)
    created = terms_mod.scaffold_terms(tmp_path)
    assert created is False
    assert path.read_text() == "real-secret-codename\n"
