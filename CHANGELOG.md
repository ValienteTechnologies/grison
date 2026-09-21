# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.3] - 2026-09-21

### Fixed

- A first `grison sync` in a fresh `git init` directory holding only a hand-placed
  `.grison/env` was refused with WS-008 before bootstrap could write the
  `.grison/.gitignore` the rule asks for. A hand-placed env no longer counts as a
  bootstrapped workspace; the run bootstraps, then the workspace rules gate it.

## [0.4.2] - 2026-09-21

### Fixed

- Slugs transliterate to ASCII instead of dropping non-ASCII letters: a report
  titled `Sızma Testi Raporu` is pulled into `sizma-testi-raporu/`, not
  `s-zma-testi-raporu/`. One shared slugify now also names inbox findings.
- A pulled wiki page file is named by BookStack's own slug for the page (as book and
  chapter directories already were), so internal links written in BookStack
  (`/books/<book>/page/<slug>`, WIKI-007) resolve against it by construction.

## [0.4.1] - 2026-09-21

### Fixed

- A record pulled from Ghostwriter whose title has surrounding whitespace or is
  empty, or whose field HTML ends in empty paragraphs/headings, no longer classifies
  as `edited` right after a clean pull (and so no longer push-loops): the remote
  canonical form now strips titles and prose exactly like the local parsers do,
  for findings, narrative sections and project notes alike. An empty remote title
  is `Untitled` on both sides and the file is `untitled.md`.
- `grison status` prints each rule id once per invalid file, with a count
  (`WIKI-010 x38, WIKI-012`), instead of once per failure line.
- Every command's `--help` is one plain sentence.

### Added

- `grison sync --allow-mass-change`: lets a deliberate bulk change (a first import,
  a whole report's worth of new findings) through the mass-change guard for that
  run only; every write is still undo-snapshotted.

## [0.4.0] - 2026-09-20

Workspace format 2. This is a **clean break, not a migration**: a workspace on
format 1 (identified by having `.grison/env` but no `.grison/manifest.yml`) is
refused outright by every command, with a plain message — nothing converts it. There
is no automatic upgrade path; re-pull the workspace fresh with a grison version that
supports format 2.

### Added

- `.grison/manifest.yml`, tracked, recording `format: 2` — the version gate a
  refused-not-migrated old workspace fails against.
- `.grison/index.json` as the sole place remote identity is recorded (path → kind +
  id) — documents themselves carry no `grison:` block, no id, and no hash.
- `docs/workspace-format.md`, the complete rule-id-bearing format spec, and a copy of
  it scaffolded into every workspace at `.grison/SPEC.md`.
- `grison validate [PATHS…] [--json] [--deleted-ok]`, a dedicated offline command with
  a three-way exit code (`0` clean, `1` real problems found, `2` could not run).
- `grison status [--remote] [--json]`, a whole-workspace offline overview (per-area
  counts, last-sync-per-phase, live collision sidecars).
- `grison scaffold [--force]`, regenerating every scaffolded workspace file on demand.
- `grison undo [SNAPSHOT] [--list]`, replaying a sync run's remote writes in reverse,
  newest write first, guarded by a pre-write re-fetch check. One snapshot per sync
  run (not per phase); the newest 10 are kept under `.grison/snapshots/`, older ones
  pruned automatically.
- A generated `CLAUDE.md`, a scaffolded `.claude/settings.json` deny-list, a
  `PostToolUse` post-edit validation hook (`grison hook post-edit`), and a git
  `pre-commit` hook — agent-proofing scaffolded into every workspace.
- `.grison/templates/` (starting points for a library finding, a reported finding, a
  wiki page, and a project note) and `.grison/terms.txt` (private, per-workspace
  confidential-term denylist, `TXT-002`).
- `findings/inbox/` and `methodology/checklists/<engagement>/` as explicit,
  fully-validated, never-synced, never-indexed local-only trees.
- Report narrative sections as one file per report per `narrative/<field>.md`, each
  with its own merge base; `.report.yml`/`project.md` as regenerated read-only
  mirrors of report metadata.
- BookStack book/chapter/shelf structure materialized as directories with read-only
  `.book.yml`/`.chapter.yml`/`.shelves/*.yml` mirrors; wiki images synced as a file
  set under `<book>/images/`, the same mechanism as report evidence.
- A Ghostwriter schema-compatibility check (cached fingerprint probe, full
  introspection fallback on drift) run once before the first fetch of any sync,
  gating on GraphQL schema features (e.g. `evidence` having no `findingId`), not on a
  version string Ghostwriter doesn't expose.
- A collision sidecar (`<name>.remote.<ext>`) as the sole on-disk representation of a
  both-sides-changed record; the local file is never overwritten.

### Changed

- Identity moved out of document frontmatter entirely: no finding, narrative section,
  note, or wiki page carries a `grison:` block, a remote id, or a sync hash — a
  document's identity is derived only from `.grison/index.json`, keyed by path.
- Evidence is referenced only as an image line in a document's body
  (`![caption](evidence/file.png "…")`); there is no `evidence:` frontmatter list.
- File and directory names under `findings/`/`methodology/` are stable handles set
  once, at pull time, and never renamed afterward — including after the remote title
  changes.
- `findings/reports/<dir>/` directory names carry no required `<id>-<slug>` prefix; a
  report's identity comes only from its `gw.report` entry in `.grison/index.json`.
- `.grison/env` credentials now sit alongside non-secret behavioral settings
  (`GRISON_GIT`, `GRISON_CLAUDE_MD`) in the same file/env precedence.

### Removed

- The `grison:` frontmatter block (kind/tier/gw-id/report-id/synced-hash) from every
  document type.
- The `evidence:` frontmatter list on findings.
- Any workspace-format-1 compatibility or migration path.

## [0.3.2]

See the [`v0.3.2`](https://github.com/ValienteTechnologies/grison/releases/tag/v0.3.2)
tag for the last format-1 release.
