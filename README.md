# grison

[![PyPI](https://img.shields.io/pypi/v/grison)](https://pypi.org/project/grison/)
[![Python](https://img.shields.io/pypi/pyversions/grison)](https://pypi.org/project/grison/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

grison mirrors [Ghostwriter](https://github.com/GhostManager/Ghostwriter) (findings,
reports, evidence) and [BookStack](https://www.bookstackapp.com/) (wiki) into a plain
markdown workspace and syncs edits back. Its job is to validate what changed and sync
it safely, whether a person or an agent made the edit.

## Install

Requires Python ≥3.11 on a POSIX system (grison uses `os.chmod`/`fcntl.flock`; it does
not run on Windows).

```sh
pipx install grison            # or: uv tool install grison
```

## Quick start

```sh
mkdir engagement && cd engagement
grison sync
```

The first run in an empty directory scaffolds the whole workspace — `findings/`,
`methodology/`, `.grison/` (with a commented credentials template at `.grison/env`),
`CLAUDE.md`, and `.claude/settings.json` — then stops and asks you to fill in
Ghostwriter (and, optionally, BookStack) credentials in `.grison/env`. Re-run
`grison sync` and it pulls every existing report, finding, and wiki page into the
tree.

`.grison/env` is created `chmod 600` and is never committed (see
[SECURITY.md](SECURITY.md)). `GRISON_*` environment variables override the file, for
CI/headless use.

## Workspace layout

```
<workspace>/
  .grison/
  CLAUDE.md
  .claude/
    settings.json
  .gitignore
  findings/
    library/
    reports/
      <report-dir>/
        evidence/
        narrative/
        notes/
        project.md
        .report.yml
    inbox/
  methodology/
    library/
      <book>/
        <chapter>/
        images/
        .book.yml
      .shelves/
    checklists/
```

A concrete example, annotated:

```
findings/
  library/
    weak-tls-config.md            # reusable finding, not tied to any report
  reports/
    acme-corp-web-assessment/     # one directory per Ghostwriter report, named from its title
      reflected-xss.md            # a reported finding
      evidence/
        xss-alert.png             # image referenced from a finding/narrative/note
      narrative/
        executive_summary.md      # one file per report extraFields section
      notes/
        follow-up.md              # a project note
      project.md  .report.yml     # read-only mirrors, regenerated every sync
  inbox/
    sql-injection.md              # `grison parse` output — triage, then `mv`/`cp`
methodology/
  library/
    web-application-testing/
      .book.yml                   # read-only mirror of the BookStack book
      recon.md                    # a page at the book root
      images/
        recon-diagram.png
      reconnaissance/
        .chapter.yml               # read-only mirror of the BookStack chapter
        subdomain-enum.md          # a page inside that chapter
    .shelves/
      pentest-methodologies.yml   # read-only mirror of a BookStack shelf
  checklists/
    acme-2026-08/                 # a per-engagement working copy of a book
```

`findings/inbox/` and `methodology/checklists/<engagement>/` are local-only: fully
validated, but never synced and never recorded in `.grison/index.json`. Everything
else in `findings/`/`methodology/` is one of the shapes below — an unrecognized file
or directory anywhere in either tree is a hard validation failure.

The complete, exact spec (every rule, with its id) is
[`docs/workspace-format.md`](docs/workspace-format.md); a copy also ships inside every
workspace at `.grison/SPEC.md`. This section is the day-to-day summary.

## Authoring rules that matter day to day

**No machine fields, no ids, ever.** No document — finding, narrative section, note,
wiki page — carries a `grison:` block, an id, or a hash. Identity (which remote record
a file corresponds to) lives entirely in the tracked `.grison/index.json`, keyed by
the file's path.

**File and directory names are stable handles.** A name you create may be anything
matching `[a-z0-9][a-z0-9._-]*` (lowercase, starts with a letter/digit). Once grison
creates a file or directory on pull, it never renames it again, even after the remote
title changes. The one exception: a file directly inside an `evidence/` or `images/`
folder keeps its name verbatim (non-ASCII, mixed case, whatever it arrived with) —
that name is still checked, just against a different, narrower rule (no path
separator, no leading dot, not shaped like a collision sidecar, valid UTF-8, ≤255
bytes).

**A finding's frontmatter:**

| field | required | notes |
|---|---|---|
| `severity` | yes | `informational`, `low`, `medium`, `high`, `critical` |
| `finding_type` | yes | `network`, `physical`, `wireless`, `web`, `mobile`, `cloud`, `host` |
| `cvss.vector` | no | a CVSS 3.0/3.1 base vector; the score is always derived |
| `cwe` | no | list of CWE ids, checked against an embedded MITRE index |
| `tags` | no | list of strings, no duplicates, no surrounding whitespace |
| `affected_entities` | no | free text — reported/inbox findings only, never `findings/library/` |

No other frontmatter key is allowed. The body is exactly one `# {title}` line, then
`## Description`, `## Impact`, `## Mitigation`, `## Replication Steps`,
`## References`, each exactly once, in that order. When `cvss.vector` is set,
`severity` must agree with its score band (inbox findings are exempt — a scanner's raw
severity rating routinely disagrees with a naive CVSS band before you've triaged it).

**A wiki page's frontmatter:** `title` (required, non-blank), `priority` (optional
integer, BookStack sort order), `tags` (optional, same shape rule as a finding's).
No other key. A page's book and chapter come from which directory it sits in — there
is no `book`/`chapter` field. The body is markdown BookStack stores and renders
verbatim: no real HTML tags (a placeholder like `<domain>` is fine), only
`http:`/`https:`/`mailto:` links, no heading-level skips, and the first heading must
not repeat the title.

**The only evidence/image form is an image line, alone in its own block:**
`![caption](evidence/file.png "optional description")` in a finding, narrative
section, or note; `![caption](images/file.png)` on a wiki page at its book's root, or
`![caption](../images/file.png)` one chapter down. A plain link to the same path —
`[see Figure 1](evidence/file.png)` — is a cross-reference, not an embed, and may
appear inline. Neither form is ever allowed in a library or inbox finding (there is
no report to hold evidence for). Removing an image line never deletes the file.

**Write a template-injection payload literally.** `{7*7}`, `{{7*7}}`, `{% debug %}`,
`${{ ... }}` and similar Jinja/Handlebars/Go-template syntax are quoted exactly as the
target reflected them, in a code span when it's code. Don't escape or "defuse" it —
grison escapes it correctly on the way to Ghostwriter.

## How sync decides

`grison sync` never asks push-or-pull: a per-record stored merge base decides.

- Only the local file changed since the last sync → **push**.
- Only the remote record changed → **pull**.
- Both changed → **collision**: the remote version is written to a `<name>.remote.<ext>`
  sidecar next to the file, the local file is never overwritten. Resolve by hand, then
  `grison sync --force-local <path>` or `--force-remote <path>`. A force flag always
  means "this side wins for this path", including on a deletion the change guard
  withheld: `--force-remote` restores a locally deleted record instead of deleting it
  remotely, `--force-local` recreates a remotely deleted one instead of deleting it
  locally.

Every record must pass `grison validate` before it's pushed, created, or deleted — the
validation gate blocks pushes, never pulls. A mass, sudden change (many records
looking edited/deleted at once, more than the change guard's threshold) withholds the
writes for that run instead of applying them, reported as `withheld`. When the bulk
change is deliberate (a first import, a whole report's worth of new findings), run
`grison sync --allow-mass-change` once: the guard steps aside for that run only, and
every write still lands in the undo snapshot.

Every remote write in one `grison sync` run — findings, report, and wiki phases alike
— shares one undo snapshot (see `grison undo` below).

`grison validate` (and, in the same spirit, `status`/`sync`) uses a three-way exit
code so a script can tell "your documents are wrong" apart from "the command itself
couldn't run":

| code | meaning |
|---|---|
| `0` | clean — nothing to fix, nothing failed |
| `1` | ran to completion and found real problems (an invalid document, a collision, a withheld mass-change, a failed sync phase) |
| `2` | could not run at all — e.g. `validate`/`status` found no workspace or were given a bad path, or `sync` hit an incompatible Ghostwriter schema before its first fetch |

## Commands

- **`grison parse <path…> [--scanner NAME] [-o DIR] [--finding-type TYPE] [--min-severity SPEC] [--dry-run]`**
  — turn a scanner export into markdown findings under `findings/inbox/` (default) or
  `-o DIR`. Fully offline; auto-detects the scanner from file content, or force it with
  `--scanner`. Supported: Acunetix, Burp Suite, Nessus, Nmap, OpenVAS, Qualys
  (network scan and WAS export alike), sslyze, OWASP ZAP.

- **`grison status [--remote] [--json]`** — a whole-workspace overview: per-area
  counts (clean/edited/new/deleted/moved/invalid/unknown), plus any live collision
  sidecar, and when each phase last synced. Offline by default; `--remote` contacts
  Ghostwriter and BookStack and dry-run classifies every phase of `grison sync`
  (report, evidence, narrative/notes, findings, wiki), printing per-kind pending
  push/pull/collision counts and problem paths without writing anything.

- **`grison validate [PATHS…] [--json] [--deleted-ok]`** — the format checker, offline,
  no credentials, no network. Runs from anywhere inside the workspace (walks up to
  `.grison/`, like `git`). Without `PATHS`, checks everything. One line per failure:
  `path:line: RULE-ID message — fix`.

- **`grison sync [--dry-run] [--force-local PATH] [--force-remote PATH] [--allow-mass-change] [--json] [--verbose]`**
  — reconcile with Ghostwriter and (if configured) BookStack. Bootstraps on first run.

- **`grison undo [SNAPSHOT] [--list]`** — reverse a whole sync run's remote writes,
  newest write first, guarded by the same pre-write re-fetch check the forward sync
  uses (a record changed since the snapshot is reported, never silently overwritten).
  One snapshot per `grison sync` run, kept under `.grison/snapshots/`; the newest 10
  are kept, older ones pruned automatically. Owner-only — the scaffolded
  `.claude/settings.json` denies an agent both `grison sync` and `grison undo`.

- **`grison scaffold [--force]`** — (re)generate every scaffolded file on demand (e.g.
  after upgrading grison): `.grison/SPEC.md`, `.grison/templates/`,
  `.grison/terms.txt`, `CLAUDE.md`, `.claude/settings.json`, the root `.gitignore`'s
  collision-sidecar entry, and (in a git repo) the `pre-commit` hook. Runs
  automatically on every `grison sync`/`grison parse` too.

## Agent-proofing

Every workspace is scaffolded to survive an AI agent editing it unsupervised:

- **`CLAUDE.md`** — generated agent instructions: layout, naming, frontmatter
  tables, the evidence/image line forms, and "run `grison validate` before you
  finish; fix every failure; never work around one." Regenerated when its marker is
  stale; a stale, hand-edited copy fails `grison validate` (WS-012) until
  `grison scaffold --force` regenerates it.
- **`.claude/settings.json` deny-list** — denies `Read` on the genuinely private
  `.grison/` entries (`env`, `state/`, `snapshots/`, `lock`, `terms.txt`), denies
  `Edit`/`Write` on all of `.grison/` and on every read-only mirror
  (`project.md`, `.report.yml`, `.book.yml`, `.chapter.yml`, `.shelves/**`), and
  denies the Bash text `grison sync`/`grison undo` in any spelling (advisory, not a
  sandbox — see the module's own caveat). Self-healing on every run.
- **A `PostToolUse` hook** (`grison hook post-edit`) validates only the file an agent
  just edited (or deleted) and prints the failures back to it. It can never block the
  edit — a `PostToolUse` hook runs after the tool call already happened — it only
  informs.
- **A git `pre-commit` hook** runs a whole-workspace `grison validate` and blocks the
  commit on failure. An existing, non-grison `pre-commit` hook is never overwritten;
  the text to add by hand is printed instead.

## Supported remotes

- **Ghostwriter ≥ 7.2.0** (feature-gated, not version-string-checked: grison verifies
  the live GraphQL schema has what its own queries/mutations need, via a cached
  fingerprint probe with a full introspection fallback on drift). Tested against
  7.2.6.
- **BookStack**, tested against the 26.05.x REST API. There is no equivalent hard
  schema gate for BookStack.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability and what grison does
(and doesn't do) with your credentials.

## Spec

The full workspace-format spec, with a rule id for every check `grison validate`
performs, is [`docs/workspace-format.md`](docs/workspace-format.md).

## License

MIT. Named after [the mustelid](https://en.wikipedia.org/wiki/Grison).
