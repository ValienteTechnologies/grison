# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities using GitHub's private vulnerability
reporting for this repository, rather than a public issue:

<https://github.com/ValienteTechnologies/grison/security/advisories/new>

This lets us discuss and fix the issue before it's public. Please do not include
real client data, hostnames, or credentials in a report — grison's own workspace data
model keeps that kind of material local (see "Data handling" below); reproduce with
synthetic data instead.

## Supported versions

Only the latest published minor version receives security fixes.

| Version | Supported |
|---|---|
| latest `0.3.x` | yes |
| anything older | no |

## Credentials

- Credentials live only in `.grison/env`, under the workspace's `.grison/` directory,
  or in `GRISON_*` environment variables (which take precedence over the file — for
  CI/headless use). There is no other place grison reads a token from.
- `.grison/env` is written `chmod 600` from the moment it's created
  (`grison/fsio.py`'s `atomic_write_text(..., private=True)` — the temp file is
  created 0600 directly, never 0644-then-chmod, so it's never briefly world/group
  readable). `grison/remote/bootstrap.py` also tightens the mode of anything already
  sitting under `.grison/` on every run.
- `.grison/env` is never committed: `.grison/.gitignore` is a fail-safe allow-list
  (ignore everything under `.grison/` except the files that must be tracked —
  `.gitignore`, `manifest.yml`, `index.json`, `SPEC.md`, `templates/`), so a stray
  edit to the workspace-root `.gitignore` can't accidentally leak it. `grison
  validate`'s `WS-008` rule fails if `.grison/env`, `.grison/state/`,
  `.grison/snapshots/`, or `.grison/terms.txt` are ever tracked, or if
  `manifest.yml`/`index.json` are ever git-ignored.
- Every Ghostwriter/BookStack base URL must be `https://` — `grison/remote/http.py`
  refuses to construct a client otherwise (`HttpConfigError`), so a credential is
  never sent over a plaintext connection.
- Grison never logs a credential or bearer token. Request/response logging is not
  performed at all; the only network-adjacent messages grison prints are sync
  outcomes (paths, record kinds, counts) and error text, never header or token
  values.
- A scaffolded `.claude/settings.json` denies an AI agent's `Read` tool on
  `.grison/env`, `.grison/state/`, `.grison/snapshots/`, `.grison/lock`, and
  `.grison/terms.txt` — see the README's "Agent-proofing" section.
- If your Ghostwriter/BookStack deployment sits behind Cloudflare Access, set
  `GRISON_CF_CLIENT_ID`/`GRISON_CF_CLIENT_SECRET` (both, or neither); the service
  token pair is sent as `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers on
  every request.

## Data handling

- `.grison/state/` (per-record sync bases), `.grison/snapshots/` (undo history, the
  newest 10 kept), `.grison/lock` (the workspace lock), and `.grison/terms.txt`
  (below) are private and stay on the machine that ran `grison sync` — none of it is
  ever committed or sent anywhere but the Ghostwriter/BookStack instance you
  configured.
- `.grison/terms.txt` is a private, per-workspace list of confidential terms (e.g. a
  real client legal name), one per line, optionally restricted to a path prefix
  (`term => findings/reports/14-acme`). `grison validate`'s `TXT-002` rule fails a
  document that uses one of these terms outside its allowed prefix, and reports the
  hit as a line number plus a **masked** form of the term (first character, then
  `*` for the rest) — the term itself is never echoed into output that could be
  committed or copied elsewhere.
- A collision sidecar (`<name>.remote.<ext>`) written when a record changed on both
  sides holds the remote's content, unencrypted, next to the local file. The
  scaffolded workspace-root `.gitignore` ignores `*.remote.*` by design (self-healing,
  like the `.claude/settings.json` deny-list) — a sidecar is never meant to be
  committed; resolve it and let the next sync remove it.
- grison performs no telemetry and makes no network requests other than to the
  Ghostwriter/BookStack URLs you configure.
