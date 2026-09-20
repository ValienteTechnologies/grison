"""Self-contained-workspace scaffolding (brief D11/D12/D13; "Workspace format v2").

Everything an agent reads in a workspace — ``.grison/SPEC.md``, ``.grison/templates/``,
``CLAUDE.md``, ``.claude/settings.json`` — is GENERATED from the same definitions the
validator enforces (:mod:`grison.formats`, :mod:`grison.validator.registry`), so it can
never drift from what ``grison validate`` actually checks. This package also installs
the two non-Claude-Code enforcement points D11 names: a git ``pre-commit`` hook and the
``.gitignore`` entry for collision sidecars.

:func:`grison.scaffold.orchestrate.scaffold_workspace` is the one entry point
:mod:`grison.remote.bootstrap` calls; ``grison scaffold [--force]`` (see ``grison/cli.py``)
calls the same function on demand.
"""

from __future__ import annotations

from grison.scaffold.orchestrate import ScaffoldResult, scaffold_workspace

__all__ = ["ScaffoldResult", "scaffold_workspace"]
