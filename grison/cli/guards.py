"""The workspace-safety gates every command runs before touching a workspace or a
remote — see :mod:`grison.cli` for the CLI itself."""

from __future__ import annotations

import fcntl
import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import typer

from grison import manifest as manifest_mod
from grison.errors import GrisonError
from grison.fsio import ensure_private_dir, open_private
from grison.remote.creds import MissingCreds
from grison.validator import WorkspaceNotFound, validate_workspace

_T2 = TypeVar("_T2")


def _guarded(fn: Callable[..., _T2]) -> Callable[..., _T2]:
    """Wrap a command so any :class:`GrisonError` reaching here prints one plain
    ``error: …`` line to stderr and exits 1, instead of an uncaught traceback (today
    e.g. ``GRISON_GW_URL=http://…`` raises ``HttpConfigError`` straight through
    typer's own rich-traceback handler). A command's own ``typer.Exit`` (its normal
    exit-code signaling) passes through untouched — this only catches what nothing
    else already handled.

    :class:`~grison.remote.creds.MissingCreds` and
    :class:`~grison.validator.WorkspaceNotFound` exit 2, not 1:
    ENGINE.md §10 puts bad/missing credentials in the same "could not run" class
    as no-workspace/incompatible-server/lock-held, never in the "ran but needs
    attention" class exit 1 is for. ``sync`` catches it itself first (to also print
    its own scaffold-status lines before exiting) — this is the backstop for every
    OTHER command that lets it reach here uncaught, e.g. ``grison undo``."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> _T2:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except (MissingCreds, WorkspaceNotFound) as e:
            typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from None
        except GrisonError as e:
            typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    return wrapper


def _is_bootstrapped(root: Path) -> bool:
    """Whether ``root`` has already been through a bootstrap (of any format,
    including a v1 workspace this grison refuses to sync — see
    :func:`_refuse_if_format_mismatch`) — see :func:`grison.manifest.
    is_bootstrapped`'s own docstring. A directory holding nothing but a
    hand-placed ``.grison/env`` (a scripted deployment, a credential file copied
    into a fresh clone — the exact case :func:`grison.remote.bootstrap.
    bootstrap_workspace` documents) is NOT bootstrapped: gating it on the WS-*
    rules first (item 1) refused every first sync in a git repo with WS-008,
    because the ``.grison/.gitignore`` that rule wants is one of the files
    bootstrap has not written yet."""
    return manifest_mod.is_bootstrapped(root)


def _refuse_if_format_mismatch(root: Path, *, bootstrapped: bool | None = None) -> None:
    """D13 (item 10, fix-fin1): every command that reads an EXISTING workspace
    refuses a format mismatch outright — exit 2, before any work (no bootstrap
    self-heal, no remote call, no validator run) — never silently proceeding as if
    the workspace were the current format. Distinct from ``grison validate``'s own
    WS-005/WS-006 failures, which report the exact same check as one finding among
    everything else `validate` found (exit 1) — this is the harder "could not run
    safely at all" gate every OTHER command needs.

    A no-op when there is nothing to check yet: a genuinely fresh directory (no
    ``manifest.yml`` AND no real v1 content — :func:`grison.manifest.
    is_bootstrapped`, the SAME precise signal ``bootstrap_workspace`` itself uses)
    bootstraps normally at ``CURRENT_FORMAT``. Checking ``.grison/`` existence
    alone would be wrong here — a directory whose ``.grison/env`` exists but has
    no ``manifest.yml`` and no real content yet (freshly written, hand-created, or
    copied in) is NOT a v1 workspace either, exactly the gap ``bootstrap_
    workspace``'s own docstring calls out; only a real ``manifest.yml`` on record,
    or real pre-v2 content, means there is an actual format to check.

    ``bootstrapped``: pass the caller's own already-computed ``is_bootstrapped``
    result to skip recomputing it — its ``has_v1_content`` half walks
    ``findings/``/``methodology/`` with an ``rglob`` when neither has a manifest
    yet. ``grison sync`` computes it once and reuses it here (this used to be a
    second, independent ``rglob`` walk of the same fresh-workspace tree); every
    other caller leaves this ``None`` and it's computed fresh here instead —
    cheap in the common already-bootstrapped case, since a v2 manifest short-
    circuits before ``has_v1_content`` ever runs."""
    if bootstrapped is None:
        bootstrapped = manifest_mod.is_bootstrapped(root)
    if not bootstrapped:
        return
    try:
        manifest_mod.check(root)
    except (manifest_mod.WorkspaceNeedsMigration, manifest_mod.WorkspaceTooNew) as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _refuse_if_workspace_rules_fail(root: Path) -> None:
    """Item 1 (CRITICAL, fix-fin1): WS-* rules (WS-011/WS-012 — a missing/hand-
    edited ``.claude/settings.json``, ``CLAUDE.md``, or ``.grison/SPEC.md``; WS-007
    a malformed manifest; WS-008 git hygiene; …) are workspace-level, not scoped to
    any one phase — before this fix, each sync phase filtered
    ``validate_workspace``'s failures down to its own path prefix
    (``findings``/``methodology``/``findings/reports``), so a WS-* failure (whose
    ``path`` is e.g. ``.claude/settings.json`` or ``CLAUDE.md``, matching none of
    those prefixes) was silently dropped by every phase and never gated anything;
    ``grison undo`` ran no validation at all. Now: ANY WS-* failure refuses the
    whole run outright — exit 2, before any remote write — "could not run safely",
    the same class as bad creds/incompatible-server/lock-held. Called BEFORE the
    self-healing ``bootstrap_workspace``/``grison scaffold`` (non-``--force``) pass
    ever runs, so a hand-edit that scaffolding would otherwise quietly re-heal is
    still caught first."""
    failures = [f for f in validate_workspace(root) if f.rule_id.startswith("WS-")]
    if not failures:
        return
    for f in failures:
        loc = f"{f.path}:{f.line}" if f.line is not None else f.path
        typer.secho(f"{loc}: {f.rule_id} {f.message} — {f.fix}", fg=typer.colors.RED, err=True)
    typer.secho(
        "workspace-level check failed — could not run safely", fg=typer.colors.RED, err=True
    )
    raise typer.Exit(code=2)


def _bootstrap_would_create(root: Path) -> list[str]:
    """The paths a real (non-``--dry-run``) first bootstrap would write in a fresh
    directory (item 2, fix-fin1) — sourced from the same constants
    ``bootstrap_workspace``/``scaffold_workspace`` themselves write, so this list
    can't silently drift from what they actually do. For reporting only: this
    function never touches the filesystem."""
    from grison.index import INDEX_RELATIVE_PATH
    from grison.scaffold import settings_json as settings_json_mod
    from grison.scaffold import spec as spec_mod
    from grison.scaffold import templates as templates_mod
    from grison.scaffold.orchestrate import CLAUDE_MD_RELATIVE_PATH
    from grison.validator.terms import TERMS_RELATIVE_PATH
    from grison.workspace import WORKSPACE_DIRS

    paths = [f"{d}/" for d in WORKSPACE_DIRS]
    paths += [
        ".grison/env",
        manifest_mod.MANIFEST_RELATIVE_PATH,
        ".grison/.gitignore",
        INDEX_RELATIVE_PATH,
        spec_mod.SPEC_RELATIVE_PATH,
        *sorted(
            f"{templates_mod.TEMPLATES_RELATIVE_DIR}/{name}"
            for name in templates_mod.all_templates()
        ),
        TERMS_RELATIVE_PATH,
        settings_json_mod.SETTINGS_RELATIVE_PATH,
        CLAUDE_MD_RELATIVE_PATH,
        ".gitignore (root — the collision-sidecar ignore entry)",
        ".git/hooks/pre-commit (only inside a git repository)",
    ]
    return paths


def _print_would_bootstrap(root: Path) -> None:
    typer.secho(
        f"{root} is not a grison workspace yet — a real (non-dry-run) "
        "`grison sync`/`grison parse` would bootstrap it first, writing:",
        fg=typer.colors.YELLOW,
    )
    for p in _bootstrap_would_create(root):
        typer.echo(f"  would create {p}")


@contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    """Serialize sync runs per workspace via an exclusive flock on .grison/lock."""
    lock_path = root / ".grison" / "lock"
    ensure_private_dir(lock_path.parent)
    fh = open_private(lock_path)
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.secho(
                "another grison sync is already running in this workspace (.grison/lock held)",
                fg=typer.colors.RED,
            )
            # ENGINE.md §10: "could not run" (no fetch has happened yet) is exit 2,
            # the same class as no-workspace/bad-creds/incompatible-server — never
            # exit 1 (that's for a run that happened but needs attention).
            raise typer.Exit(code=2) from None
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
