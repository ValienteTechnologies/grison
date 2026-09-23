# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.3] - 2026-09-21

### Added

- The converter grammar accepts fenced code blocks (at top level and as their
  own block inside a list item), blockquotes and GFM tables, the three constructs
  Ghostwriter's editor supports and real reports use. Canonical HTML follows
  TipTap; Ghostwriter's own variants are accepted on pull. Column alignment,
  `colspan`/`rowspan` and the table caption node are dropped with loss reporting.
  A malformed table, an unmatched backtick in a cell, a nested blockquote, or a
  table/blockquote inside a list item are refused with a plain message. Proven
  against the lab Ghostwriter (`proofs/converter-grammar-lab.md`).
- `grison validate` now catches two Ghostwriter server-side evidence rejections
  offline, before any push: `REF-009` (a file in a report's `evidence/` directory
  has an extension outside Ghostwriter's own allow-list — `txt`, `md`, `log`,
  `jpg`, `jpeg`, `png` — case-insensitive; the message suggests `.gif` -> `.png`,
  `.html`/`.csv` -> `.txt`) and `REF-010` (an embed's caption is over
  Ghostwriter's 255-character `Evidence.caption` limit). Both limits live in one
  place, `grison.remote.ghostwriter.limits`, imported by the validator, the
  evidence adapter (a defense-in-depth check at push time) and the fake
  Ghostwriter test server (same rejection wording as the real one). An over-long
  caption that reaches `grison sync` anyway degrades to "no local caption
  opinion" and self-heals the same way an `REF-004` caption conflict already
  does — the evidence file still uploads, the referencing document's on-disk
  caption is rewritten to match, and its push still goes through in the same
  run; a bad extension has no such repair and is refused outright.
- A real scanner export corpus, vendored from DefectDojo and reptor (provenance
  and licenses in `tests/fixtures/scanners/ATTRIBUTION.md`), backs a golden-IR
  test and a `grison parse` -> `grison validate` contract test for every scanner,
  alongside the existing hand-made samples.
- Qualys: the `ASSET_DATA_REPORT` export (its VM/network-scan format, distinct
  from the report export already supported) is now detected and parsed. WAS
  exports also yield `INFORMATION_GATHERED` items, matching DefectDojo's own
  counts on its sample reports.
- Burp and ZAP scanner imports carry the vendor's own confidence rating as a
  `confidence:<value>` tag (lowercased — Burp: `certain`/`firm`/`tentative`; ZAP:
  `false-positive`/`low`/`medium`/`high`, its own 0..3 scale) — the first use of
  the `<namespace>:<value>` convention for scanner-derived tags. When a scanner
  reports the same finding more than once with different confidence, the highest
  (most certain) is kept. Documented in `docs/workspace-format.md`'s tags section.

### Changed

- Internal layout: the five oversized modules (`cli`, `markdown/converter`,
  `validator/core`, `engine/apply`, `engine/filesets`) and the Ghostwriter client
  and the `gw_findings`, `gw_report` and `bs_pages` adapters are packages now, one
  file per responsibility, with what the two engines and the three sync phases
  repeated collapsed into one place each. Public import paths are unchanged. Tests
  mirror the source tree. One behaviour change: a failure syncing one book's
  `images/` no longer aborts the other books' images and pages (report evidence
  already had that isolation).
- `grison parse` refuses a file it recognises but won't turn into findings,
  reported under its own header, and the run exits `1`: nmap exports (they are
  reconnaissance output, not findings; host/port inventory will get its own
  place later) and sslyze JSON from before v5. Nmap input used to produce an
  informational finding per open port; it now produces none.
- A finding's severity is the maximum across every occurrence it aggregates, for
  every scanner. Acunetix and Burp/ZAP already worked this way; Nessus, Qualys
  and OpenVAS used to keep whichever occurrence was seen first.
- Qualys's unknown-severity fallback is `INFO`, the same as every other scanner
  (previously scanner-specific).

### Removed

- `grison/migrate/` (the one-time wiki cleanup used for the v1 -> v2 move): the
  migration is done; the code and its tests are gone.
- `finding_guidance` (OpenVAS's `vuldetect` tag) dropped from the scanner IR:
  workspace format 2 has no field for it, and it was never read past the parser
  that set it.
- The nmap grepable format and `ImportOptions.fmt` are gone along with nmap's
  finding output; only nmap's XML export is recognised (and refused, see above).

### Fixed

- A first `grison sync` in a fresh `git init` directory holding only a hand-placed
  `.grison/env` was refused with WS-008 before bootstrap could write the
  `.grison/.gitignore` the rule asks for. A hand-placed env no longer counts as a
  bootstrapped workspace; the run bootstraps, then the workspace rules gate it.
- A scan file that fails to parse, can't be read, or is invalid UTF-16 now makes
  `grison parse` exit `1` instead of `0`; it's reported under its own header,
  once, the same way an invalid finding already was.
- A UTF-8 or UTF-16 byte-order mark, or leading whitespace before `<?xml`, no
  longer stops a scan file from being detected and parsed.
- A generic XML root (`report`, `results`, `issues`) is only attributed to
  OpenVAS or Burp when that scanner's own markers are present, instead of by
  root element name alone.
- Qualys's `--min-severity` filter now runs per occurrence, before aggregation,
  so a QID is no longer dropped just because its first-seen host was below the
  threshold.
- References are HTML-escaped and turned into clickable links for every
  scanner, not just some.
- Acunetix CWE was always empty: real exports carry `<CWEList><CWE id="200">CWE-200
  </CWE></CWEList>`, not the flat `<CWE>` the parser only ever read (which real
  Acunetix XML doesn't emit). The parser now reads the first `CWEList/CWE` id,
  falling back to a flat `<CWE>` if present.
- Acunetix CVSS extraction stays v3-first, but a finding with only a v4 vector
  (`<CVSS4><Descriptor>`, no `<CVSS3><Descriptor>`) no longer silently loses it:
  `grison parse` now warns `finding <title>: only a CVSS v4 vector was present,
  dropped (v4 not supported in workspace format 2)` instead of leaving no trace.
- OpenVAS titles containing an embedded newline are rendered on a single line.
- `<b>`/`<i>` in scanner-supplied HTML (real Burp/ZAP output, e.g. `<b>X-Frame-
  Options</b>`) used to fall back to plain-text degrade, losing the emphasis —
  `grison.markdown.mapping` now rewrites them to `<strong>`/`<em>` via an
  HTML-aware pass before conversion, so they come out as real markdown bold/
  italic.
- The fallback text used when a scanner field carries an HTML tag outside the
  supported set came through raw and unescaped: an entity-escaped payload (e.g.
  a literal `<script>`) reappeared as a live tag, and entities were unescaped
  twice. It now goes through the same escaping the converter uses everywhere
  else.
- A plain-text line from scanner prose that starts with `|` or is shaped like a
  thematic break (`---`, `***`, `___`) now round-trips instead of being misread
  as a table row or a horizontal rule.
- ZAP's `replication_steps` interpolated an alert instance's `uri`/`method`/
  `param` into `<li>` markup unescaped; an instance uri carrying its own markup
  (e.g. a reflected-XSS payload like `</style/</title/...`, which real ZAP
  exports quote back verbatim) produced malformed HTML that `grison parse`
  couldn't convert, dropping the payload text from the rendered finding
  entirely. Every interpolated instance field is now `html.escape`d
  (`grison/scanners/zap.py`); `sslyze.py`'s weak-key description escapes its
  certificate public-key fields the same way, for consistency. With both fixes,
  every prose field of every Burp/ZAP fixture converts without falling back to
  the plain-text degrade.

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
