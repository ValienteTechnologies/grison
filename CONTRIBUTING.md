# Contributing to grison

## Dev setup

Python ≥3.11, managed with [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # installs the dev dependency group (pytest, hypothesis, ruff, mypy, …)
uv run pytest -q        # run the test suite
```

## Gates

Every change must pass, locally and in CI:

```sh
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
uv run mypy grison            # type checking
uv run pytest -q              # unit + property + e2e tests
```

CI (`.github/workflows/ci.yml`) runs all four gates above — `ruff check`, `ruff
format --check`, `mypy grison`, and `pytest` — on 3.11, 3.12, and 3.13.

## How tests are organized

- **Unit tests** (`tests/<area>/test_*.py`) — one module's own behavior, grouped into
  subpackages that mirror `grison/`'s own layout: `tests/cli/`, `tests/engine/`,
  `tests/formats/`, `tests/markdown/`, `tests/migrate/`, `tests/model/`,
  `tests/remote/`, `tests/scaffold/`, `tests/scanners/`, `tests/sinks/`,
  `tests/validator/`. Cross-cutting, top-level `grison/` modules (`fsio`, `hashing`,
  `index`, `manifest`, `gitdrive`, `settings`, `slug`, plus the `smoke` and
  `spec_coverage` tests) live in `tests/core/`. Each subpackage has an `__init__.py`
  so `tests.<area>.test_x` stays importable; `tests/_ws2_helpers.py` stays at the top
  of `tests/` because both `tests/validator/` and `tests/scaffold/` import it.
- **Property tests** (`hypothesis`, e.g. `tests/markdown/test_converter_property.py`,
  `tests/formats/test_formats_fuzz.py`, `tests/engine/test_engine_identity.py`) —
  generated inputs proving an invariant (a round-trip, a fixpoint, "never assigns one
  record twice") rather than one example at a time.
- **End-to-end tests** (`tests/e2e/`) — the real CLI (`typer.testing.CliRunner` against
  `grison.cli.app`) driven against in-memory fake Ghostwriter/BookStack servers
  (`tests/fakes/gw_server.py`, `tests/fakes/bs_server.py`), with only the `httpx`
  transport swapped out — no real network, no real credentials. `tests/conftest.py`
  wires the `workspace`/`gw_server`/`bs_server` fixtures together and applies across
  every subpackage.
- **Spec↔rule coverage** (`tests/core/test_spec_coverage.py`) — every rule id
  registered in `grison/validator/registry.py` must appear in
  `docs/workspace-format.md`, and must have both a `@pytest.mark.rule("XXX-nnn")`
  failing-case test and a `@pytest.mark.rule_ok("XXX-nnn")` passing-case test
  somewhere in the suite (a retired rule id is exempt from the test requirement but
  must never appear on a marker). This test fails the build if you add or change a
  validator rule without updating the spec and writing both cases.

## Commit style

Conventional commit prefixes: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`,
`infra`. Imperative, present tense subject lines.

## Release process

Releases are automated (`.github/workflows/release.yml`) — do not hand-tag.

1. Bump `VERSION` and push to `main`.
2. The `release` workflow triggers on that push, runs the test suite, builds, and
   publishes to PyPI via trusted publishing (OIDC).
3. Only after a successful publish does CI derive the tag from `VERSION` (`v<VERSION>`)
   and push it.

A tag pushed by hand instead of by this workflow can leave a red run that can't be
re-run (the publish step's own duplicate-version guard treats it as already done) —
always release by bumping `VERSION`, never by creating a tag directly.

## No breaking-compat shims

Workspace format 2 is a clean break from format 1: an old-format workspace is refused
outright by every command (`WS-005`), with no migration path and no compatibility
shim. Don't add code that tries to interpret an older format directly — a workspace
stays on the grison version that wrote it until it's re-pulled fresh. The same
applies going forward: a future format 3 should refuse format 2 outright too, not
grow a translation layer inside grison itself.
