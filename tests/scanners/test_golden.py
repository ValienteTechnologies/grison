"""Golden IR tests over the real scanner-export corpus.

Parametrised over every file under ``tests/fixtures/scanners/<scanner>/`` (the
hand-made ``*_sample.*`` fixtures plus the vendored DefectDojo/reptor corpus —
see ``tests/fixtures/scanners/ATTRIBUTION.md``). For each file this records what
``grison.scanners.detect_bytes`` and the matching parser do *today*, run through
the same ``normalise_input`` step (see ``grison.scanners.detect``) the real
pipeline (``grison/sinks/pipeline.py``) applies before both detection and
parsing — so this module records the production contract, not a raw-bytes
approximation of it. This records current behaviour, bugs included — this
module is a behaviour recorder, not a correctness check.

Regenerate every expected file after an intentional parser change with::

    pytest tests/scanners/test_golden.py --update-golden

The ``--update-golden`` flag is registered in the root ``tests/conftest.py``
and, since it's a plain pytest option, also works against the whole suite
(``pytest --update-golden``) or any narrower selection.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from grison.scanners import ImportOptions, RefusedInput, detect_bytes, scanner_for
from grison.scanners.detect import normalise_input
from grison.scanners.ir import ScanFinding

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_EXPECTED = _FIX / "expected"
# The eight parser slugs (grison.scanners.SCANNERS) double as the fixture
# subdirectory names under tests/fixtures/scanners/.
_SCANNER_DIRS = ("acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap")


def _discover_fixtures() -> list[Path]:
    files: list[Path] = []
    for scanner in _SCANNER_DIRS:
        d = _FIX / scanner
        if d.is_dir():
            files.extend(sorted(p for p in d.iterdir() if p.is_file()))
    return files


def _finding_to_dict(f: ScanFinding) -> dict[str, Any]:
    d = dataclasses.asdict(f)
    d["severity"] = f.severity.value  # StrEnum -> its plain string value
    return d


def _run(path: Path) -> dict[str, Any]:
    # Same normalisation the pipeline applies once and feeds to both detection
    # and parsing (grison/sinks/pipeline.py) — this records the production
    # contract, not raw-bytes behaviour.
    data = normalise_input(path.read_bytes())
    name = detect_bytes(data)
    doc: dict[str, Any] = {"detected": name, "outcome": "ok", "error": None, "findings": []}
    if name is None:
        return doc

    cls = scanner_for(name)
    assert cls is not None, f"{name!r} came from detect_bytes but has no registered parser"
    try:
        findings = cls().parse(data, ImportOptions())
    except RefusedInput as e:
        # A distinct outcome from "error": the parser recognised this input and
        # made a deliberate call not to process it (see RefusedInput's docstring
        # in grison/scanners/base.py) — recorded with the plain message, not the
        # exception-type-plus-first-line shape a genuine parse error gets below.
        doc["outcome"] = "refused"
        doc["error"] = str(e)
        return doc
    except Exception as e:  # noqa: BLE001 — recording current behaviour, not filtering it
        first_line = (str(e).splitlines() or [""])[0]
        doc["outcome"] = "error"
        doc["error"] = f"{type(e).__name__}: {first_line}"
        return doc

    doc["findings"] = [_finding_to_dict(f) for f in findings]  # parser's own order, not resorted
    return doc


def _expected_path(fixture: Path) -> Path:
    return _EXPECTED / fixture.parent.name / f"{fixture.name}.ir.json"


_FIXTURES = _discover_fixtures()
_IDS = [f"{p.parent.name}/{p.name}" for p in _FIXTURES]


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_golden(fixture: Path, update_golden: bool) -> None:
    actual = json.dumps(_run(fixture), indent=2, sort_keys=True) + "\n"
    expected_path = _expected_path(fixture)

    if update_golden:
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(actual)
        print(f"regenerated {expected_path.relative_to(_FIX)}")
        return

    if not expected_path.is_file():
        pytest.fail(f"no golden at {expected_path} — run with --update-golden to create it")

    expected = expected_path.read_text()
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(expected_path),
                tofile=f"actual ({fixture.parent.name}/{fixture.name})",
            )
        )
        pytest.fail(f"golden mismatch for {fixture.parent.name}/{fixture.name}:\n{diff}")
