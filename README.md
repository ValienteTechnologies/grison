# grison

[![PyPI](https://img.shields.io/pypi/v/grison)](https://pypi.org/project/grison/)
[![Python](https://img.shields.io/pypi/pyversions/grison)](https://pypi.org/project/grison/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

grison turns raw security-scanner exports into clean markdown findings, gives you a
plain-files workspace to triage and edit them in, and syncs the result into
[Ghostwriter](https://github.com/GhostManager/Ghostwriter) (findings + evidence) and
[BookStack](https://www.bookstackapp.com/) (methodology) with a git-style 3-way merge.

Markdown in the middle is the point: findings are files, so the transform layer is
whatever edits files best — a human in any editor, a script, or an LLM let loose on the
workspace. grison itself has no AI subsystem; its job is to validate what changed and
sync it safely.

Supported scanner exports (auto-detected): Acunetix, Burp, Nessus, Nmap, OpenVAS,
Qualys, sslyze, ZAP.

## Install

```sh
pipx install grison            # or: uv tool install grison
grison --install-completion    # optional: shell tab completion (once, then restart shell)
```

For development: `uv sync && uv run pytest` (Python 3.11+, managed with
[uv](https://docs.astral.sh/uv/)).

## Quickstart

```sh
mkdir engagement && cd engagement
grison parse scan.nessus burp.xml    # first run scaffolds the workspace
# triage findings/inbox/*.md, edit freely, move the keepers into place:
cp findings/inbox/sqli-login.md findings/reports/6-acme-q3/
grison status findings/              # validity report per record
grison sync                          # reconcile with Ghostwriter + BookStack
```

## The three verbs

| Command | Does |
|---|---|
| `grison parse <path…>` | scanner export(s) → markdown findings in `findings/inbox/`. Offline. Auto-detects the scanner (`--scanner` to force), `--min-severity high,critical` or `medium-critical` to filter, `--dry-run` to preview. |
| `grison status <path…>` | per-record validity: schema, enums, CVSS vector↔score, CWE ids, Ghostwriter HTML whitelist. Offline. |
| `grison sync` | reconcile the workspace with Ghostwriter (findings) and BookStack (methodology). Direction is derived per record — see below. `--dry-run` previews the plan. |

There is deliberately no `pull`/`push` (sync derives direction), no `init` (the first
run of any verb scaffolds the workspace), and no `validate` (status reports, sync
enforces). Moving a finding between tiers is a plain `cp`/`mv`.

## The workspace

The directory tree is the data model — location is identity:

```
findings/
  inbox/
  library/
  reports/<id>-<slug>/
    narrative/
    evidence/
methodology/
  library/<book>/<chapter>/
  checklists/<engagement>/
.grison/
```

| Path | Mirrors | Notes |
|---|---|---|
| `findings/inbox/` | nothing | parse output; triage here, then `cp` into a tier |
| `findings/library/` | Ghostwriter finding library | reusable templates |
| `findings/reports/<id>-<slug>/` | Ghostwriter reported findings | one dir per *existing* report; grison never creates reports |
| `findings/reports/…/narrative/` | Ghostwriter report `extraFields` | one editable markdown file per report section (exec summary, methodology, …); 3-way per section. `.report.yml` holds the read-only metadata mirror |
| `findings/reports/…/evidence/` | Ghostwriter evidence | images attached to a finding |
| `methodology/library/` | BookStack books/chapters/pages | markdown-native, mirrors verbatim |
| `methodology/checklists/` | nothing | per-engagement working copies, `cp -r` from library |
| `.grison/` | — | creds + sync state; auto-gitignored, never commit |

BookStack's structure mirrors losslessly: `library/<book>/<page>.md` for pages at a
book's root, `library/<book>/<chapter>/<page>.md` for chaptered pages. Every book and
chapter — including empty ones — materializes as a directory holding a `.book.yml` /
`.chapter.yml` mirror (ids, name, description, shelf membership, chapter order;
pull-only). Page sort order (`priority`) and page tags live in the page frontmatter
and sync in both directions. Moving a file between chapter directories moves the page
on BookStack; a page moved into a chapter remotely relocates the local file on the
next sync.

Sync matches records by remote id stored in the file, not by filename (filenames are
cosmetic). A file whose directory disagrees with its stored id is a *move* and becomes
a new record at the destination.

## Finding schema

One tier-agnostic schema: structured facts in YAML frontmatter, prose in fixed `##`
sections.

```markdown
---
grison:
  kind: finding
  tier: instance            # library | instance
  gw: { table: reportedFinding, id: 183, report_id: 6 }
  synced: { hash: sha256:…, at: 2026-07-14T12:00:00Z }  # the 3-way merge base
severity: high              # informational|low|medium|high|critical
finding_type: web           # network|physical|wireless|web|mobile|cloud|host
cvss: { vector: "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", score: 9.8 }
cwe: ["CWE-79"]             # validated against an embedded CWE index
affected_entities: |        # instances only
  https://app.example/
evidence:                   # instances only
  - { file: evidence/shell.png, caption: Shell, friendly_name: shell, gw: { id: 38, hash: sha256:… } }
---

# {title}
## Description
## Impact
## Mitigation
## Replication Steps
## References
```

The pydantic models in `grison/model/` are the schema. CVSS is accepted as well-formed
3.0 or 3.1, as authored. CWE ids are validated against a vendored index of the MITRE
catalog, so everything works fully offline.

## How sync decides direction

Nobody chooses push or pull — the stored merge base (`synced.hash`) determines it per
record:

- only local changed → **push**
- only remote changed → **pull**
- both changed → **collision**: the remote version is written to an `x.remote.md`
  sidecar, local is never overwritten; resolve, then
  `grison sync --force-local <file>` or `--force-remote <file>`
- both converged under a stale base → repair the base, write nothing

## Guardrails

Guardrails stop anomalous or destructive outcomes, never routine ones — no
confirmation prompts, `--dry-run` is opt-in. Three layers:

1. **Validation** — silent when green; a record must be valid to sync.
2. **Trip-wires** — fire only on anomaly: mass-change guard, structure drift,
   collision, duplicate identity, broken evidence link.
3. **Snapshots** — every remote write batch is snapshotted first, with a paired
   `rollback.py`, so every write is reversible.

## Markdown ⇄ Ghostwriter HTML

Ghostwriter's rich-text fields use a small closed vocabulary, and the converter fails
loudly on anything outside it rather than corrupt silently. Inline: `**bold**`,
`` `code` ``, `*em*`, `[links](…)`. Block: paragraphs and unordered lists. Rejected
inside a field: tables, ordered lists, images, headings (the `##` section headers are
grison structure mapping to Ghostwriter's separate fields, not field content).
BookStack pages are markdown-native and mirror verbatim, no conversion.

## Configuration

The first run in a directory scaffolds `.grison/env` — a commented template, chmod 600,
auto-gitignored. Paste values there, or set the same keys as environment variables
(env vars override the file; useful for CI):

| Key | For |
|---|---|
| `GRISON_GW_URL`, `GRISON_GW_TOKEN` | Ghostwriter — GraphQL API token |
| `GRISON_BS_URL`, `GRISON_BS_TOKEN_ID`, `GRISON_BS_TOKEN_SECRET` | BookStack — REST API token; only needed for methodology sync |
| `GRISON_CF_CLIENT_ID`, `GRISON_CF_CLIENT_SECRET` | optional — Cloudflare Access service token, if your deployment sits behind CF Access |

## License

MIT. Named after [the mustelid](https://en.wikipedia.org/wiki/Grison).
