"""The remote-client construction seam — see :mod:`grison.cli` for the CLI itself.

Every command/phase module that needs a Ghostwriter or BookStack client calls
``clients._make_gw_client(...)``/``clients._make_bs_client(...)`` through this module
object (never a bare ``from grison.cli.clients import _make_gw_client``), so tests can
monkeypatch ``grison.cli.clients._make_gw_client``/``_make_bs_client`` once and have it
take effect everywhere a client is built (``sync``, ``status --remote``, ``undo``)."""

from __future__ import annotations

from grison.remote.bookstack import BookStackClient
from grison.remote.creds import Creds
from grison.remote.ghostwriter import GhostwriterClient


def _make_gw_client(creds: Creds) -> GhostwriterClient:
    """Build the Ghostwriter client — the seam tests monkeypatch to inject a
    transport (``httpx.MockTransport``) instead of hitting the network."""
    return GhostwriterClient(creds)


def _make_bs_client(creds: Creds) -> BookStackClient:
    """Build the BookStack client — same seam as :func:`_make_gw_client`."""
    return BookStackClient(creds)
