"""``.grison/SPEC.md`` is package data (task item 1): it must ship inside the real
wheel, and ``docs/workspace-format.md`` (the human-facing, spec-coverage-tested copy)
must stay byte-identical to it.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from grison.scaffold.spec import spec_text

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_spec_text_matches_the_package_data_file() -> None:
    on_disk = (_REPO_ROOT / "grison" / "scaffold" / "data" / "workspace-format.md").read_text(
        encoding="utf-8"
    )
    assert spec_text() == on_disk


def test_docs_copy_is_byte_identical_to_the_package_copy() -> None:
    docs_copy = (_REPO_ROOT / "docs" / "workspace-format.md").read_text(encoding="utf-8")
    package_copy = (_REPO_ROOT / "grison" / "scaffold" / "data" / "workspace-format.md").read_text(
        encoding="utf-8"
    )
    assert docs_copy == package_copy, (
        "docs/workspace-format.md has drifted from grison/scaffold/data/workspace-format.md "
        "(the package copy is canonical — see grison/scaffold/spec.py's module docstring); "
        "copy the package file over the docs file to fix"
    )


def test_spec_md_is_included_in_the_built_wheel(tmp_path: Path) -> None:
    """Builds a REAL wheel and inspects the archive — proof, not a claim, that
    ``.grison/SPEC.md``'s content actually ships, not just that it exists on disk in
    this checkout."""
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    result = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out_dir)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"uv build failed:\n{result.stdout}\n{result.stderr}"

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        assert "grison/scaffold/data/workspace-format.md" in names
        packaged = zf.read("grison/scaffold/data/workspace-format.md").decode("utf-8")
    assert packaged == spec_text()
