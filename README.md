# grison

grison is a markdown hub between security scanners and Ghostwriter + BookStack. It parses
scanner exports into a single house schema of markdown findings, and reconciles a local
git-like workspace with Ghostwriter (findings, "GW") and BookStack (methodology) via a
3-way sync. Markdown is the lingua franca — which also makes an LLM editing the workspace
the transform layer, so grison itself has no AI subsystem; its job is to validate and sync
those edits safely.

Supported scanner exports (auto-detected): Acunetix, Burp, Nessus, Nmap, OpenVAS, Qualys,
sslyze, ZAP.

## Install

```sh
pipx install grison       # or: uv tool install grison
grison --help
```

For development:

```sh
uv sync                   # Python 3.11+, managed with uv
uv run pytest
```

## The three verbs

| Command | Does |
|---|---|
| `grison parse <path…>` | scanner export(s) → markdown findings in `findings/inbox/` (offline; auto-detects the scanner) |
| `grison status <path…>` | per-record validity: schema / enums / CVSS / CWE / the GW HTML whitelist |
| `grison sync` | reconcile the workspace with Ghostwriter + BookStack; direction derived per record |

There is no `pull`/`push` (sync derives direction), no `init` (the first `sync`
bootstraps the workspace + a `.grison/env` creds template), and no `validate` (status
reports it, sync enforces it). Moving a finding between cells is a plain `cp`/`mv`.

## Data model — a 2×2, mirrored faithfully

The workspace tree *is* the model. Segment 1 = domain (backend), segment 2 = tier:

```
findings/                 # ⇄ Ghostwriter
  inbox/                  # parse output — local-only, triage then cp into a report
  library/                # ⇄ GW finding table
  reports/<id>-<slug>/    # ⇄ GW reportedFinding — one dir per EXISTING report
    <rfid>-<slug>.md
    evidence/*.png        # images attached to a finding
methodology/              # ⇄ BookStack
  library/<book>/<page>.md
  checklists/<engagement>/  # per-engagement copies — local-only, cp -r from library/
.grison/                  # creds + state; always gitignored
```

**Location is identity.** A file's directory fixes its remote target; sync matches by
remote id, not filename (filenames are cosmetic). A file whose location disagrees with
its stored id is a *move* → a new record — which is all the old
`instantiate`/`promote` verbs did. grison syncs findings + evidence into reports it
never creates.

## Finding markdown schema (by example)

One tier-agnostic schema; structured facts in frontmatter, prose in fixed `##` sections.

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
cwe: ["CWE-79"]             # validated against the embedded CWE index
affected_entities: |        # instances only
  https://app.example/
evidence:                   # instances only; images attached to this finding
  - { file: evidence/shell.png, caption: Shell, friendly_name: shell, gw: { id: 38, hash: sha256:… } }
---

# {title}
## Description
## Impact
## Mitigation
## Replication Steps
## References
```

The pydantic models in `grison/model/` **are** the schema; enums carry their Ghostwriter
ids (derived, never stored). CVSS accepts well-formed 3.0 or 3.1 as-authored.

## Converter whitelist (markdown ⇄ Ghostwriter HTML)

GW's rich-text fields use a tiny closed vocabulary, and the converter (`grison/markdown/`)
fails loudly on anything outside it rather than corrupt silently:

- Inline: `**bold**` ⇄ `<strong>`, `` `code` `` ⇄ `<code>`, `*em*` ⇄ `<em>`, `[t](u)` ⇄ `<a>`.
- Block: paragraphs ⇄ `<p>`, `- ` lists ⇄ `<ul><li>` (never `<ol>`).
- Rejected in a field: tables, ordered lists, images, headings. The `##` section headers
  are grison structure that map to GW's separate fields, not field content.

(BookStack methodology pages are markdown-native, so they mirror verbatim — no converter.)

## Sync + guardrails

Direction isn't chosen — the 3-way base (`synced.hash`) *determines* it per record:
only-local-changed → push, only-remote → pull, both → **collision** (the remote side is
written to an `x.remote.md` sidecar; local is never overwritten — resolve then
`sync --force-local/--force-remote <file>`), converged-under-a-stale-base → repair.

Guardrails stop **anomalous or destructive outcomes**, never routine ones (no
confirmation nagging; `--dry-run` is opt-in). Three layers: validation (silent when
green), trip-wires that fire only on anomaly (mass-change guard, structure drift,
collision, duplicate identity, broken link), and a pre-write **snapshot** of every remote
write batch (with a paired `rollback.py`) so every write is reversible.
