"""The one base HTTP client both :class:`~grison.remote.ghostwriter.GhostwriterClient`
and :class:`~grison.remote.bookstack.BookStackClient` build on: ``httpx`` client
construction, Cloudflare Access headers, ``transport=`` injection for tests,
context-manager methods, an ``https://``-only base-URL guard, and retry-with-backoff
for idempotent failures.

Retry policy: a call marked ``idempotent=True`` (every GET; every Ghostwriter
GraphQL *query*) retries, with exponential backoff and jitter, on a connection
error/timeout, HTTP 429 (honouring ``Retry-After`` when the server sends one), and
502/503/504. A call marked ``idempotent=False`` (every GraphQL *mutation*; every
BookStack POST/PUT/DELETE) never blind-retries on a response — it may already have
been applied server-side — but a pre-send failure (``httpx.ConnectError`` /
``httpx.ConnectTimeout``, which prove the request never reached the server at all)
is retried even for a mutation, since nothing could have happened remotely yet.

Max attempts, base delay, and the ``sleep`` callable are constructor parameters so
tests can run every retry path with zero real sleep.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from grison.errors import GrisonError
from grison.remote.creds import Creds

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class HttpConfigError(GrisonError, ValueError):
    """A remote client was configured with something that must be refused before
    any request is made (today: a non-``https://`` base URL)."""


class BaseHttpClient:
    """Shared ``httpx.Client`` construction + retry loop. Subclasses (one per
    remote) add their own request-shaping and response/error handling on top."""

    def __init__(
        self,
        creds: Creds,
        *,
        base_url: str,
        url_setting_name: str,
        headers: dict[str, str],
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if not base_url.startswith("https://"):
            raise HttpConfigError(
                f"{url_setting_name} must be an https:// URL, got {base_url!r} — "
                "grison refuses to send credentials over a non-https connection"
            )
        self._client = httpx.Client(
            base_url=base_url,
            headers={**headers, **creds.cf_headers()},
            timeout=timeout,
            transport=transport,
        )
        self._max_attempts = max(1, max_attempts)
        self._base_delay = base_delay
        self._sleep = sleep
        self._random = random_fn

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """Send one request, retrying per the module docstring's policy.
        ``idempotent`` defaults to ``True`` for GET and ``False`` for everything
        else; callers (GraphQL POSTs, which are always method ``POST`` regardless
        of query-vs-mutation) pass it explicitly."""
        if idempotent is None:
            idempotent = method.upper() == "GET"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.request(method, path, params=params, json=json)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # the request never reached the server — safe to retry even a
                # mutation, since nothing could have been applied remotely yet
                if attempt >= self._max_attempts:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            except httpx.TimeoutException:
                # any other timeout (read/write/pool) can't prove the server
                # didn't receive and act on the request — only retry if the call
                # itself is declared idempotent
                if not idempotent or attempt >= self._max_attempts:
                    raise
                self._sleep(self._backoff(attempt))
                continue

            if (
                idempotent
                and resp.status_code in _RETRYABLE_STATUSES
                and attempt < self._max_attempts
            ):
                self._sleep(self._retry_delay(resp, attempt))
                continue
            return resp

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter: ``base * 2**(attempt-1)`` plus up
        to one more ``base`` of random jitter, so concurrent retries don't
        synchronize on the same schedule."""
        return self._base_delay * (2.0 ** (attempt - 1)) + self._random() * self._base_delay

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass  # not a delay-seconds value (e.g. an HTTP-date) — fall back
        return self._backoff(attempt)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BaseHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
