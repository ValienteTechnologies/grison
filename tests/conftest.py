"""Root pytest config plus shared e2e fixtures: fake remotes, a scratch workspace,
and a CLI runner wired to both through the ``grison.cli.clients`` transport seam
(``_make_gw_client``/``_make_bs_client``).

No test under ``tests/e2e/`` needs the network: ``run_grison`` invokes the real CLI
(``typer.testing.CliRunner`` against ``grison.cli.app``) with both remote clients'
``httpx`` transports monkeypatched to the in-memory fakes in ``tests/fakes/``.

Also registers ``--update-golden`` (used by ``tests/scanners/test_golden.py`` to
rewrite ``tests/fixtures/scanners/expected/**/*.ir.json`` instead of asserting
against it) at the repo root, since a pytest option must be registered in a
``conftest.py`` at or above every path it's invoked against — ``pytest
--update-golden`` (no path restriction) only works from a root-level conftest.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import grison.cli as cli_mod
import grison.cli.clients as cli_clients_mod
from grison import manifest as manifest_mod
from grison.index import Index
from grison.remote.bookstack import BookStackClient
from grison.remote.compat import fingerprint_from_schema, save_cache
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient
from tests.fakes.bs_server import FakeBookStack
from tests.fakes.gw_server import FakeGhostwriter
from tests.fakes.gw_server import load_schema as load_fake_gw_schema


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite tests/scanners/test_golden.py's expected files instead of asserting.",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


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
    # A real fresh bootstrap (grison/remote/bootstrap.py) writes manifest.yml/
    # index.json/.grison/.gitignore together with .grison/env — this fixture
    # pre-seeds env by hand (for the fake creds above) without going through that
    # path, so it writes the same trio itself; every e2e workspace is format v2 from
    # the start, matching what a real first `grison sync` would produce (never a v1
    # workspace needing migration, which manifest.read() would otherwise read this
    # as — see bootstrap.py's own comment on the same gap).
    manifest_mod.write(root)
    manifest_mod.write_gitignore(root)
    Index(root=root).save()
    # Pre-seed grison.remote.compat's schema-fingerprint cache from the SAME SDL
    # FakeGhostwriter itself validates every request against
    # (tests/fixtures/gw-schema-7.2.6.graphql via tests.fakes.gw_server.load_schema)
    # so every e2e sync in this suite takes the warm (one-request) compat-check
    # path by default instead of paying for a full introspection + validate-every-
    # operation pass every single time — see tests/test_remote_compat.py for the
    # tests that deliberately exercise the cold path instead.
    save_cache(root, fingerprint_from_schema(load_fake_gw_schema()))
    return root


def tree_snapshot(root: Path, *, exclude: frozenset[str] = frozenset()) -> dict[str, bytes]:
    """Every file under ``root`` (workspace tree + ``.grison/`` alike), path ->
    exact bytes — for asserting a command wrote nothing at all (e.g. ``status
    --remote``'s dry-run classify, or a sync refused before its first fetch): two
    snapshots taken before/after must compare equal. ``exclude`` names relative
    paths to leave out entirely (e.g. ``.grison/state/mirrors.json``, which
    ``grison.scaffold.orchestrate._scaffold_spec`` unconditionally re-records on
    EVERY ``sync``/``parse`` call — including one a workspace lock then refuses —
    a pre-existing, unrelated-to-the-lock quirk, not something the caller is
    testing)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and str(p.relative_to(root)) not in exclude
    }


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
        # sleep=lambda: no-op so an e2e test exercising a retry path stays fast
        return GhostwriterClient(creds, transport=gw_server.transport, sleep=lambda _: None)

    def make_bs(creds: Creds) -> BookStackClient:
        return BookStackClient(creds, transport=bs_server.transport, sleep=lambda _: None)

    monkeypatch.setattr(cli_clients_mod, "_make_gw_client", make_gw)
    monkeypatch.setattr(cli_clients_mod, "_make_bs_client", make_bs)
    monkeypatch.chdir(workspace)

    runner = CliRunner()

    def _run(*args: str) -> Result:
        return runner.invoke(cli_mod.app, list(args))

    return _run
