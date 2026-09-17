"""Shared e2e fixtures: fake remotes, a scratch workspace, and a CLI runner wired to
both through the ``grison.cli`` transport seam (``_make_gw_client``/``_make_bs_client``).

No test under ``tests/e2e/`` needs the network: ``run_grison`` invokes the real CLI
(``typer.testing.CliRunner`` against ``grison.cli.app``) with both remote clients'
``httpx`` transports monkeypatched to the in-memory fakes in ``tests/fakes/``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import grison.cli as cli_mod
from grison.remote.bookstack import BookStackClient
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient
from tests.fakes.bs_server import FakeBookStack
from tests.fakes.gw_server import FakeGhostwriter


@pytest.fixture
def gw_server() -> FakeGhostwriter:
    """A fresh in-memory fake Ghostwriter, seeded with just the lookup tables every
    sync needs (severities/finding-types/content-types) — no findings/reports."""
    return FakeGhostwriter()


@pytest.fixture
def bs_server() -> FakeBookStack:
    """A fresh in-memory fake BookStack — no books/pages seeded."""
    return FakeBookStack()


@pytest.fixture
def workspace(tmp_path: Path, gw_server: FakeGhostwriter, bs_server: FakeBookStack) -> Path:
    """A scratch workspace dir with ``.grison/env`` pointing at the fakes' (fake)
    URLs and the fakes' own tokens — the URLs are never dialed (the CLI's transport
    seam routes every request straight into the in-memory handlers), they only need
    to be non-empty so :meth:`Creds.require_ghostwriter`/``require_bookstack`` pass."""
    root = tmp_path / "workspace"
    grison_dir = root / ".grison"
    grison_dir.mkdir(parents=True)
    env_path = grison_dir / "env"
    env_path.write_text(
        "GRISON_GW_URL=https://fake-ghostwriter.invalid\n"
        f"GRISON_GW_TOKEN={gw_server.token}\n"
        "GRISON_BS_URL=https://fake-bookstack.invalid\n"
        f"GRISON_BS_TOKEN_ID={bs_server.token_id}\n"
        f"GRISON_BS_TOKEN_SECRET={bs_server.token_secret}\n"
    )
    env_path.chmod(0o600)
    return root


@pytest.fixture
def run_grison(
    workspace: Path,
    gw_server: FakeGhostwriter,
    bs_server: FakeBookStack,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Result]:
    """Invoke the real CLI against ``workspace``, both remotes routed to the fakes.

    ``run_grison("sync", "--dry-run")`` behaves exactly like running
    ``grison sync --dry-run`` from a shell in ``workspace`` — same argument parsing,
    same output, same exit code — except every Ghostwriter/BookStack HTTP call is
    served in-process by ``gw_server``/``bs_server``.
    """

    def make_gw(creds: Creds) -> GhostwriterClient:
        return GhostwriterClient(creds, transport=gw_server.transport)

    def make_bs(creds: Creds) -> BookStackClient:
        return BookStackClient(creds, transport=bs_server.transport)

    monkeypatch.setattr(cli_mod, "_make_gw_client", make_gw)
    monkeypatch.setattr(cli_mod, "_make_bs_client", make_bs)
    monkeypatch.chdir(workspace)

    runner = CliRunner()

    def _run(*args: str) -> Result:
        return runner.invoke(cli_mod.app, list(args))

    return _run
