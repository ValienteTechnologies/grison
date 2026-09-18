"""``.grison/SPEC.md`` — a verbatim copy of the workspace-format spec, shipped as
package data.

The canonical text lives at ``grison/scaffold/data/workspace-format.md`` (a real,
committed file inside the package, so it is a plain file on disk in an editable/dev
checkout AND is picked up automatically by hatchling's default wheel builder, the same
way ``grison/model/data/cwe.json`` already is — see ``tests/test_scaffold_spec.py``,
which builds a real wheel with ``uv build --wheel`` and inspects the archive to prove
it). ``docs/workspace-format.md`` (read by humans, and by
``tests/test_spec_coverage.py``, which needs a fixed, human-facing path) is kept as a
byte-identical generated mirror of the package copy — ``tests/test_scaffold_spec.py``
also asserts the two are identical, so the two can never silently drift apart.

This is the simplest robust option available: an *editable* install (``uv sync``/
``pip install -e .``) points straight at the source tree, so a file that only appeared
via a build-time step (e.g. hatchling's ``force-include``, which maps an external path
into the wheel but does nothing for an editable install) would not exist on disk for
:mod:`importlib.resources` to find while running from a dev checkout — exactly the mode
grison's own test suite (and every contributor's ``uv run grison``) runs in. Making the
package copy the single real, always-present file sidesteps that gap entirely.
"""

from __future__ import annotations

from importlib import resources

SPEC_RELATIVE_PATH = ".grison/SPEC.md"
_PACKAGE = "grison.scaffold.data"
_RESOURCE = "workspace-format.md"


def spec_text() -> str:
    """The exact text ``.grison/SPEC.md`` is scaffolded with."""
    return resources.files(_PACKAGE).joinpath(_RESOURCE).read_text(encoding="utf-8")
