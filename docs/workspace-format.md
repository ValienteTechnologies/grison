# grison workspace format v2

This is the complete, exact spec for everything an agent writes into a grison
workspace. It is written so that an agent with **only this file** — no other grison
source — can write a valid document of every type. Every rule below has an id
(`WS-…`, `FND-…`, `REP-…`, `WIKI-…`, `REF-…`, `TXT-…`, `IDX-…`); `grison validate`
reports exactly these ids, and grison's own test suite fails if any rule here has no
validator test, or any validator rule has no section here
(`tests/test_spec_coverage.py`).

**Closed world.** Most of this workspace is written by AI agents, not humans typing at
a keyboard. Every rule below is therefore a hard failure — there are no warnings, no
`--force` flags, and no autofix. Anything not explicitly allowed is rejected. If a rule
here doesn't cover a shape you want to write, that shape is not allowed; restructure
the content to fit an allowed shape instead.

**No machine fields.** No document in this workspace — finding, narrative section,
note, wiki page — carries a `grison:` block, an id, a hash, or any other value a human
author didn't write. Identity (which remote record a file corresponds to) lives
entirely in the tracked `.grison/index.json`, keyed by the file's **path**. Tier, kind,
and location are derived from where a file sits in the tree, never from its content.

**File and directory names are stable handles, not derived data.** A new file or
directory an author creates may be named anything matching
`[a-z0-9][a-z0-9._-]*` (lowercase, must start with a letter or digit; files also need
the correct extension for their location). Once grison creates a file or directory on
pull, it never renames it again — not when the remote title changes, not after a
create gets its id.

---

## 1. Workspace layout

```
<workspace>/
  .grison/                     private, grison-owned (agents may not write here)
      .gitignore                allow-list — ignores everything except the tracked
                                 files below
      manifest.yml               TRACKED  format: 2
      index.json                 TRACKED  path -> remote identity
      SPEC.md                    TRACKED  a copy of this file
      templates/                 TRACKED  starting points for new files
      env  state/  snapshots/  terms.txt   private, never tracked
  CLAUDE.md                     generated agent instructions
  .claude/settings.json         scaffolded deny-list + post-edit hook
  .gitignore                    must not ignore .grison/ wholesale
  findings/
    library/*.md                 library findings
    reports/<id>-<slug>/         one dir per Ghostwriter report (grison-created only)
        <name>.md                 reported findings (one file per finding)
        evidence/                 evidence files of that report
        narrative/<field>.md      report narrative sections
        notes/*.md                project notes
        project.md  .report.yml   read-only mirrors, regenerated every sync
    inbox/                       local-only scanner output — see §1.4
  methodology/
    library/<book>/[<chapter>/]*.md   wiki pages
    library/<book>/.book.yml           read-only book mirror
    library/<book>/<chapter>/.chapter.yml  read-only chapter mirror
    library/.shelves/<slug>.yml        read-only shelf mirrors
    library/<book>/images/<file>       wiki images (D9)
    checklists/                        local-only — see §1.4
```

### 1.1 Names (`WS-001`)

Every file or directory name under `findings/` or `methodology/` (except a
dotfile grison itself writes, like `.report.yml` or `.book.yml`) must match
`[a-z0-9][a-z0-9._-]*`: lowercase ASCII, start with a letter or digit, then any run of
lowercase letters, digits, `.`, `_`, `-`. No spaces, no uppercase, no leading dot (for
an author-created name), no leading hyphen.

- **Fails (`WS-001`):** `findings/library/My Finding.md` (space, uppercase).
- **Passes:** `findings/library/reflected-xss-search.md`.

### 1.2 Unknown paths (`WS-002`, `WS-003`)

Everything under `findings/` and `methodology/` must be one of the shapes this
document describes — including under the local-only trees (§1.4): a `findings/inbox/`
or `methodology/checklists/` file is not synced, but its LAYOUT is checked exactly
like everywhere else. An unrecognized file or directory anywhere in either tree is a
hard failure — `WS-002` under `findings/`, `WS-003` under `methodology/`. This
includes: a stray file dropped directly in a report directory, a subdirectory of
`findings/library/` or `findings/inbox/` (both only ever hold `*.md` files directly),
a non-`.md` file directly inside `narrative/` or `notes/`, an unexpected entry
directly inside a book/chapter directory or a checklist engagement/chapter directory,
a non-`.yml` file (or a directory) directly inside `.shelves/`, and anything at all
inside `findings/` or `methodology/` that isn't `library/`, `reports/`, `inbox/`
(findings) or `library/`, `checklists/` (methodology).

- **Fails (`WS-002`):** `findings/reports/1-acme/scratch.txt`.
- **Passes:** the same content moved into `findings/reports/1-acme/notes/scratch.md`.

### 1.3 Report directories

A `findings/reports/` entry's directory name is a stable handle, not derived data
(same rule as any other file/directory name, §1.1) — it carries **no required id
prefix**. grison names a freshly-pulled report directory `slug(title)` with no id at
all (e.g. `findings/reports/acme-corp/`); an existing v1 `<id>-<slug>` name (e.g.
`findings/reports/14-acme-corp/`) is equally valid and is never renamed afterward. A
report directory's identity comes ONLY from its `gw.report` entry in
`.grison/index.json` — a document sitting inside a report directory that isn't
indexed that way is `IDX-003` (§7), not a name-shape rule.

*(`WS-004` — retired: an earlier draft of this rule required an `<id>-<slug>` shape.
Retracted because it directly contradicted the rule above; see the appendix.)*

### 1.4 Local-only trees: validated, never synced, never indexed

`findings/inbox/` (`grison parse` output, triaged by hand into `findings/library/` or
a report directory) and `methodology/checklists/<engagement>/` (a per-engagement
working copy, `cp -r` from `methodology/library/<book>`) are never synced and never
appear in `.grison/index.json` — but every other rule in this document still applies
to them in full. Agents write here just as much as anywhere else in the workspace, so
"local-only" means "not synced," never "not validated":

- **`findings/inbox/*.md`** — flat, `*.md` only (§1.2). Each file is a finding
  document, tier `inbox`: every `FND-…` rule applies (§2), with two narrow
  exceptions — no image lines (§2.5/§5.1, same as a library finding, since there is
  no report yet to hold evidence for) and no `FND-015` (severity-vs-CVSS-band; see
  §2.3's note on why an inbox finding is exempt). It carries no machine field of any
  kind — same as every other tier; `grison parse` writes plain v2 documents.
- **`methodology/checklists/<engagement>/[<chapter>/]*.md`** — same shape as a real
  book (§4), validated with the full `WIKI-…` rule set including banned text (§8).
  Internal links (`WIKI-007`) and images (§5.2) resolve against the checklist's own
  copy, PLUS the real `methodology/library/` namespace (an inherited link/image in
  copied content still legitimately names the original library book it came from).
  A checklist's `.book.yml`/`.chapter.yml` are copies, not regenerated mirrors: they
  are still schema-checked (`WS-010`) but exempt from the hand-edited check
  (`WS-009`) — editing them is expected.
- **Never indexed**: a `findings/inbox/…` or `methodology/checklists/…` path
  appearing in `.grison/index.json` at all is `IDX-002` (§7) — neither tree
  corresponds to any real remote record.

### 1.5 Format version (`WS-005`, `WS-006`)

`.grison/manifest.yml` records `format: 2`. A workspace whose format differs from the
one this grison supports is refused outright, with a plain message — nothing converts
it, and grison never tries to interpret a workspace at another format directly. If
it's older, `grison validate` fails with `WS-005`; if it's newer than this grison
understands, `WS-006` says to upgrade grison. A workspace with no `manifest.yml` but
with a real `.grison/env` reads as format 1 (pre-dates the manifest file) and gets
`WS-005`; one with neither reads as a fresh, unbootstrapped directory (not validated
as a workspace at all — that's what `grison sync`'s bootstrap step is for).

### 1.6 `.grison/manifest.yml` schema (`WS-007`)

```yaml
format: 2
```

One required integer key, `format`. Anything else — missing, not a mapping, `format`
not an integer — is `WS-007`. This file is grison-owned; never hand-edit it.

### 1.7 Git hygiene (`WS-008`)

When the workspace is a git repository, `.grison/env`, `.grison/state/`,
`.grison/snapshots/`, and `.grison/terms.txt` must be git-ignored (a leak here is
credentials or private confidential-term data), and `.grison/manifest.yml` /
`.grison/index.json` must NOT be git-ignored (losing either silently breaks every
future sync's identity tracking). Either direction wrong is `WS-008`. grison writes
`.grison/.gitignore` itself as a fail-safe allow-list — ignore everything in the
directory except the files that must be tracked — so a stray edit to the top-level
`.gitignore` can't accidentally leak or lose either kind of file.

### 1.8 `.grison/index.json`

```json
{
  "version": 1,
  "records": {
    "findings/library/reflected-xss.md": {"kind": "gw.finding", "id": 41},
    "findings/reports/14-acme/evidence/screenshot-1.png": {"kind": "gw.evidence", "id": 501}
  }
}
```

Keys sorted, one record per line (git-merge-friendly), written atomically. Kinds:
`gw.finding`, `gw.reportedFinding`, `gw.report` (the report directory itself),
`gw.reportSection` (one `narrative/<field>.md` — a section has no id of its own in
Ghostwriter, since `report.extraFields` is a single jsonb map, not a table; its
`.grison/index.json` id is `report_id * 100000 + extraFieldSpec.id`, decodable and
unique per report+field — see `grison.adapters.gw_report.section_id`), `gw.evidence`,
`gw.projectNote`, `bs.shelf`, `bs.book` (the book directory itself), `bs.chapter`
(the chapter directory itself), `bs.page`, `bs.image`. A structurally malformed
`index.json` (not the shape above, an unknown kind, a non-integer id, the same
identity indexed under two paths) is `IDX-001` — see §7. This file is grison-owned
and tracked; never hand-edit it.

### 1.9 Read-only mirrors (`WS-009`, `WS-010`)

`.report.yml`, `project.md`, an *indexed* `notes/*.md` file, `.book.yml`,
`.chapter.yml`, and `.shelves/*.yml` are all regenerated by grison on every sync and
must never be hand-edited. `grison validate` compares each one's current content
against a digest grison recorded the last time it generated that exact file
(`.grison/state/mirrors.json` — see `grison/validator/mirrors.py`); a mismatch is
`WS-009` ("do not edit; it is regenerated on every sync"). Before the first sync ever
runs, no digest is recorded yet — the check passes (there is nothing to compare
against). Independently, each of these files (except `project.md`, which is opaque
prose — see §3.3) has its own schema (§3.4, §4.4); a file that doesn't parse into it
is `WS-010`.

---

## 2. Finding documents (`FND-…`)

Applies to `findings/library/*.md` (tier `library`), every `*.md` file directly
inside a `findings/reports/<dir>/` (tier `instance`), and every `*.md` file directly
inside `findings/inbox/` (tier `inbox` — local-only, §1.4) — tier comes from location
only.

### 2.1 Shape

```markdown
---
severity: high
finding_type: web
cvss:
  vector: CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H
cwe:
  - CWE-79
tags:
  - injection
affected_entities: |
  https://app.example.com/search
---
# Reflected XSS in the search field

## Description

The `q` parameter is reflected into the page without encoding.

## Impact

An attacker can run arbitrary JavaScript in a victim's session.

## Mitigation

HTML-encode all reflected user input.

## Replication Steps

1. Browse to `/search?q=<script>alert(1)</script>`.
2. Observe the alert fires.

![Alert firing](evidence/xss-alert.png "the alert dialog")

## References

- https://owasp.org/www-community/attacks/xss/
```

Frontmatter fields, all optional except `severity` and `finding_type`:

| field | type | notes |
|---|---|---|
| `severity` | one of `informational`, `low`, `medium`, `high`, `critical` | required |
| `finding_type` | one of `network`, `physical`, `wireless`, `web`, `mobile`, `cloud`, `host` | required |
| `cvss.vector` | a CVSS 3.0/3.1 base vector string | the score is always derived, never authored |
| `cwe` | list of CWE ids (`CWE-79`, or bare `79`) | must exist in the embedded MITRE index |
| `tags` | list of strings | no duplicates (case-insensitive), no surrounding whitespace |
| `affected_entities` | free text | **instance/inbox tier only** |

No other frontmatter key is allowed (`FND-001`) — in particular there is no `evidence:`
list (D1: the only authored evidence form is an image line in the body, §5), and no
`grison:` block on ANY tier, including `inbox`: `grison parse` writes plain v2
documents with no machine fields at all, and a hand-added `grison:` block anywhere is
`FND-001` like any other unrecognized frontmatter key.

Body: exactly one `# {title}` line, followed by exactly these five `##` sections, in
exactly this order, every one present exactly once:

1. `## Description`
2. `## Impact`
3. `## Mitigation`
4. `## Replication Steps`
5. `## References`

- Missing any of the five: `FND-009`.
- A `##` heading that isn't one of the five: `FND-010`.
- A `##` heading repeated: `FND-011`.
- The five present but out of order: `FND-012`.
- No `# {title}` line before the first `##`, or a blank title: `FND-013`.
- Text sitting outside every section (before the title, or between the title and the
  first `##`): `FND-016`.
- The frontmatter itself isn't valid YAML, or the document has no frontmatter fence at
  all: `FND-017`.

### 2.2 Field validity

- `FND-002` — a required field (`severity`, `finding_type`) is missing.
- `FND-003` — `severity` isn't one of the five allowed values.
- `FND-004` — `finding_type` isn't one of the seven allowed values.
- `FND-005` — `cvss.vector` doesn't parse as a well-formed CVSS 3.0/3.1 base vector
  (grammar, required metrics, legal values — see `grison/model/cvss.py`). CVSS 2.0 and
  4.0 are not supported (D8).
- `FND-006` — a `cwe` entry doesn't normalize to an id in the embedded MITRE index.
- `FND-007` — `tags` has a duplicate (case-insensitive), an empty string, or a string
  with leading/trailing whitespace.
- `FND-008` — `affected_entities` is set on a **library** finding (D1/instance-only).

### 2.3 Cross-field: severity vs. the CVSS band (`FND-015`, library/instance only)

When `cvss.vector` is present, `severity` must match the band its score falls in:

| score | required severity |
|---|---|
| 0.0 | `informational` or `low` |
| 0.1 – 3.9 | `low` |
| 4.0 – 6.9 | `medium` |
| 7.0 – 8.9 | `high` |
| 9.0 – 10.0 | `critical` |

grison has no CVSS "None" severity tier the way NVD does, so a 0.0 score accepts
either `informational` or `low`. Every other band is exact — a `9.5` vector with
`severity: high` is `FND-015`.

**This rule does not apply to the `inbox` tier.** A scanner's own severity rating is
independent of any CVSS vector it also reports, and routinely disagrees with the
vector's naive base-score band — a real example from the vendored OpenVAS fixture:
`severity: medium` against `CVSS:3.1/.../C:N/I:N/A:N`, whose base score is `0.0`
(informational/low band). Requiring agreement before an agent has triaged the finding
would make `grison parse`'s own routine output invalid. The agent is expected to
reconcile severity and CVSS before moving the finding into `library`/a report, where
this rule then applies in full.

### 2.4 Body converts to Ghostwriter HTML (`FND-014`)

Every one of the five sections is pushed to Ghostwriter as a rich-text field, so each
one's markdown must convert with the real converter
(`grison.markdown.converter.md_to_html`, `headings=False` for a finding — headings are
reserved for report narrative, §3). §6 quotes the converter's exact supported grammar
and normalizations verbatim. Anything outside that grammar — a table, an ATX heading, a
literal `<div>`, an unsupported Jinja/legacy construct — is `FND-014`, naming the
construct and the line where possible.

### 2.5 Images (`REF-…`, §5)

Neither a library nor an inbox finding may contain an image line at all (`REF-005`) —
there is no report to hold evidence for either. An instance finding's image lines are
validated by §5.

---

## 3. Report-owned files (`REP-…`)

### 3.1 Narrative sections

`findings/reports/<dir>/narrative/<field>.md` — one file per instance-defined
`extraFieldSpec` row on the Report model (Ghostwriter's `internalName` column is the
field key; `position` is the intended reading order, recorded in `.report.yml`'s
`narrative_order` — §3.4). No frontmatter at all; the filename stem is the field key.
The body is markdown in the same vocabulary as a finding section, **plus ATX
headings** (`md_to_html(..., headings=True)` — ATX headings `#`-`######` are allowed
here, unlike in a finding section), and may embed report evidence the same way a
finding section does (§5.1). A body that doesn't convert is `REP-001`.

```markdown
The engagement ran from 2026-08-01 to 2026-08-14 against the scoped web application.

## Summary of findings

Three critical, two high findings were identified.
```

A `narrative/<field>.md` whose stem is not one of `.report.yml`'s recorded
`narrative_order` entries is `REP-003` — the validator's only (offline, no
Ghostwriter contact) way to tell a real section from a leftover/typo'd one. When
`.report.yml` has never been written yet (no sync has run for this report), or its
`narrative_order` is empty, there is nothing recorded to compare against and
`REP-003` never fires for that report — the same "nothing to compare against yet"
convention `WS-009` uses for a mirror digest.

### 3.2 Project notes

`findings/reports/<dir>/notes/*.md` — two shapes share this directory, told apart
**without any id in the file**, purely by whether the path is listed in
`.grison/index.json` (D3):

- **A mirrored note** (an existing Ghostwriter project note, regenerated read-only
  every sync — its path is indexed) carries optional `author`/`timestamp`
  frontmatter (display metadata, never an identity field) and a body:

  ```markdown
  ---
  author: Jordan Lee
  timestamp: '2026-08-05'
  ---

  Client confirmed the maintenance window for Thursday.
  ```

- **A new local note** (not yet pushed — its path is NOT indexed) is bare markdown,
  **no frontmatter fence at all**:

  ```markdown
  Ask the client whether the staging environment is in scope too.
  ```

`REP-002` fires when the shape disagrees with index membership: an indexed note with
no frontmatter, or an unindexed note that starts with a `---` fence, or frontmatter
with an unrecognized field. A note's body (either shape) is validated exactly like a
narrative section (`REP-001` if it doesn't convert — a note is pushed as rich text too).

### 3.3 `.report.yml` / `project.md` (read-only mirrors)

`.report.yml` is plain YAML (no frontmatter fence):

```yaml
title: Acme Corp — External Pentest
project:
  id: 88
  client:
    id: 4
    name: Acme Corp
    short_name: acme
  start_date: '2026-08-01'
  end_date: '2026-08-14'
status:
  complete: false
  archived: false
  delivered: false
dates:
  creation: '2026-08-01T00:00:00Z'
  last_update: '2026-08-10T00:00:00Z'
narrative_order:
  - about_us
  - executive_summary
  - attack_chain
  - methodology
  - disclaimer
  - scope_text
  - appendix
```

`project.md` is opaque regenerated prose (scope, objectives, targets, white cards);
there is no schema beyond "it's text" — its integrity is entirely the `WS-009` digest
check (§1.9). Neither file is ever hand-authored.

### 3.4 `.report.yml` schema (`WS-010`)

Top-level keys: `title` (string), `project` (`id`, `client.id`/`client.name`/
`client.short_name`, `start_date`, `end_date`), `status` (`complete`, `archived`,
`delivered` — booleans), `dates` (`creation`, `last_update`), `narrative_order` (list
of strings — the report's `extraFieldSpec` field names in `position` order, §3.1). No
other top-level or nested key. A `.report.yml` that doesn't match this shape is
`WS-010`.

---

## 4. Wiki pages (`WIKI-…`)

`methodology/library/<book>/[<chapter>/]*.md`, and — every rule in this section
applies identically — `methodology/checklists/<engagement>/[<chapter>/]*.md` (§1.4;
never synced, never indexed, but validated in full). A page's book/engagement and
chapter come from its directory only (D4) — there is no `book`/`chapter` frontmatter
key.

### 4.1 Frontmatter

```markdown
---
title: Recon methodology
priority: 10
tags:
  - recon
  - external
---
Start with passive reconnaissance: `whois`, certificate transparency logs, and
search-engine dorking.
```

Exactly three optional-except-`title` keys:

| field | type | notes |
|---|---|---|
| `title` | non-blank string | required |
| `priority` | integer | BookStack sort order; omit to leave it to BookStack |
| `tags` | list of strings | same shape rule as a finding's `tags` (§2.2) |

- `WIKI-001` — any other frontmatter key (no `grison:` block, no page id — the id
  lives in `.grison/index.json`).
- `WIKI-002` — `title` missing or blank.
- `WIKI-003` — `priority` present but not an integer.
- `WIKI-004` — `tags` shape invalid (same rule as `FND-007`).
- `WIKI-014` — the frontmatter itself isn't valid YAML, or there's no frontmatter
  fence at all.

### 4.2 Body — BookStack takes it verbatim

There is no converter for a wiki page: BookStack stores and renders the markdown as
written. The validator therefore enforces the following directly (all offline, via
`markdown-it-py`), instead of deferring to a whitelist:

- **`WIKI-005` — real HTML is rejected**, but an angle-bracket *placeholder* is not
  HTML and must pass. The rule: an `html_block`/`html_inline` construct markdown-it-py
  reports is only rejected when the tag name it names is a **known HTML5 element name**
  (the full list is in `grison/validator/wikibody.py`'s `_KNOWN_HTML_TAGS`). `<domain>`,
  `<user>`, `<ip>` are not known tag names, so `<domain>` in prose **passes**; `<div>`,
  `<span>`, `<table>`, `<script>` are known tag names, so they **fail**. A comment,
  doctype, CDATA section, or processing instruction is always real HTML regardless of
  tag name. None of this applies **inside a code span or fenced code block** — code
  content is never HTML-parsed by markdown-it in the first place, so `` `<domain>` ``
  and a fenced block full of `<placeholder>` tokens always pass.
- **`WIKI-006` — only `http:`, `https:`, `mailto:` links.** A link or image whose
  destination has ANY other scheme — `file:`, `javascript:`, `data:`, `ftp:`, a custom
  scheme — is rejected. A schemeless (relative) destination is not covered by this
  rule; see `WIKI-007`/`REF-007` instead.
- **`WIKI-007` — an internal BookStack link must resolve.** A link shaped like
  `/books/<book-slug>/page/<page-slug>` or `/books/<book-slug>/chapter/<chapter-slug>`
  (or, when the workspace's BookStack host is known from `.grison/env`, an absolute
  URL to that same host with one of those paths) must name a book directory that
  exists in this workspace, and — for a page/chapter link — a page/chapter whose
  current local file/directory name matches. **Known limitation**: grison names a page
  file by its slug *only at pull time* and never renames it afterward (D4's "stable
  handle" rule), so after the page is renamed in BookStack, an internal link using its
  new slug cannot be resolved offline — the validator only knows the slug as of the
  last pull. This is inherent to offline, no-network validation.
- **`WIKI-008` — no zero-width or bidi control characters** anywhere in the body:
  U+200B/200C/200D/FEFF/2060 (zero-width space/joiners/BOM/word-joiner), U+200E/200F
  (LRM/RLM), U+202A–202E (embedding/override), U+2066–2069 (isolates).
- **`WIKI-009` — no CRLF.** The file must use LF line endings only.
- **`WIKI-010` — no trailing whitespace** on any line.
- **`WIKI-011` — exactly one trailing newline.** Not zero, not two or more.
- **`WIKI-012` — no heading-level skips.** A heading may only go one level deeper than
  the deepest heading seen so far (`# ` then `### ` with no `## ` in between is a
  skip); going back up any number of levels is always fine.
- **`WIKI-013` — the first heading must not repeat the title.** BookStack already
  shows the page's title; a first heading whose text case-insensitively matches
  `title` is redundant and rejected.

### 4.3 Images (`REF-…`, §5)

A wiki image line is validated by §5 (D9 — the same mechanism as evidence, D1).

### 4.4 `.book.yml` / `.chapter.yml` / `.shelves/*.yml` schemas (`WS-010`)

```yaml
# <book>/.book.yml
name: Web Application Testing
slug: web-application-testing
description: Methodology pages for web app engagements.
tags:
  - web
shelves:
  - pentest-methodologies
```

```yaml
# <book>/<chapter>/.chapter.yml
name: Reconnaissance
slug: reconnaissance
description: ''
tags: []
priority: 10
```

```yaml
# .shelves/<slug>.yml
name: Pentest methodologies
slug: pentest-methodologies
description: ''
tags: []
books:
  - web-application-testing
  - network-testing
```

All three: `name`, `slug`, `description` (strings), `tags` (list of strings). `.book.yml`
additionally has `shelves` (list of slugs); `.chapter.yml` additionally has `priority`
(int or absent); `.shelves/*.yml` additionally has `books` (list of slugs, in shelf
order). `description` and `tags` are new in format v2 — a later sync-engine step
populates them from BookStack; today they may simply be absent/empty. No other key.
A file that doesn't match its schema is `WS-010`. A checklist's own `.book.yml`/
`.chapter.yml` (§1.4) are copies, not regenerated mirrors — still schema-checked
(`WS-010`) but exempt from the hand-edited check (`WS-009`).

---

## 5. References (`REF-…`) — D1 (evidence) / D9 (wiki images)

One shared mechanism, two adapters: a folder (`evidence/` per report, `images/` per
book) mirrors a remote file set; an image line in a document body is a *reference*
into it. Removing a reference never deletes the file (that's a sync-engine concern);
these rules are purely about whether a reference is well-formed and resolves.

Every `REF-…` rule below is decided from `grison.markdown.refscan.scan_refs`'s output
— a real markdown-it token stream, not a line-level regex — so an image or link
written inside a fenced code block or an inline code span is never treated as a
reference at all (code content is never inline-parsed by markdown-it in the first
place), and each finding's `standalone`/position is exactly what the real converter
would also decide, computed once and reused rather than re-derived from a converter
error message.

### 5.1 Evidence (findings, narrative sections, notes)

An embed — `![caption](evidence/file.png "optional description")` — is valid only as
a top-level block on its own, or as its own block inside one list item (nothing else
in that paragraph/list-item block). A plain link to an evidence path —
`[text](evidence/file.png)` — is the cross-reference form ("see Figure N").

- `REF-001` — an image is not alone in its own block (e.g. mid-sentence).
- `REF-002` — an embed's `evidence/…` path doesn't resolve to a file in that report's
  `evidence/` directory.
- `REF-003` — two files in the same `evidence/` (or `images/`, §5.2) directory share a
  stem (name without extension) — e.g. `shot.png` and `shot.jpg` together.
- `REF-004` — the same file is embedded with two different non-empty captions
  somewhere in the same report (or the same book, for a wiki image) — an empty
  caption never conflicts (D1: "empty alt = keep the existing caption").
- `REF-005` — a **library or inbox** finding contains an image line at all (neither
  has a report to hold evidence for).
- `REF-006` — a cross-reference link's `evidence/…` target doesn't resolve.

```markdown
![Login form before the fix](evidence/login-before.png "captured 2026-08-03")

See [Figure 1](evidence/login-before.png) for the vulnerable state.
```

### 5.2 Wiki images (D9)

The only allowed image form in a wiki page is `![caption](images/<file>)`, and its
path must resolve to `<book>/images/<file>` — but the **relative prefix depends on
where the page sits**: exactly one spelling is correct per location.

| page location | correct spelling |
|---|---|
| book root (`<book>/*.md`) | `images/<file>` |
| inside a chapter (`<book>/<chapter>/*.md`) | `../images/<file>` |

- `REF-007` — the image uses the wrong spelling for its location (e.g. a chapter page
  writing `images/x.png` instead of `../images/x.png`), or a spelling that doesn't
  match either accepted shape at all.
- `REF-002` — (reused) the resolved path doesn't name a real file in `<book>/images/`.
- `REF-003`/`REF-004` — same rules as §5.1, scoped to one book's `images/` directory.

```markdown
<!-- methodology/library/web-application-testing/reconnaissance/subdomains.md -->
![Subdomain enumeration results](../images/subdomain-scan.png)
```

---

## 6. The converter's supported grammar (quoted, not re-described)

Every finding section and narrative field is pushed through
`grison.markdown.converter.md_to_html`. Its module docstring is the authoritative
grammar and canonicalization list; `FND-014`/`REP-001` fire whenever a document falls
outside it. Quoted verbatim from `grison/markdown/converter.py`:

> Whitelist (both directions):
>   block:  ``<p>`` <-> paragraph, ``<ul><li>`` <-> ``- `` list item,
>           ``<ol><li>`` <-> ``1. `` list item (numbered sequentially on emit;
>           ``<ol start="N">`` <-> the first item's literal number)
>   inline: ``<strong>`` <-> ``**bold**``/``__bold__``, ``<code>`` <-> `` `code` ``
>           (a longer backtick fence is used when the code text itself contains a
>           run of backticks), ``<em>`` <-> ``*em*``/``_em_``,
>           ``<strong><em>`` <-> ``***both***``,
>           ``<a href[ title]>`` <-> ``[text](url[ "title"])`` (a literal ``)`` in the
>           URL and backslash-escaped punctuation in text both round-trip),
>           ``<br>`` <-> a hard line break inside a paragraph (every markdown newline
>           in a paragraph — bare, or CommonMark's own ``\\`` / trailing-two-spaces
>           hard-break syntax — is treated as one; grison has no soft-break concept).
>   ``<span>`` is unwrapped (kept, tag dropped) rather than rejected, since TinyMCE
>   wraps highlighted text in it. An ``<ol type="...">`` (a/i/A/I numbering style) is
>   dropped the same way — markdown can't represent it — and reported via
>   ``on_loss``.
>
> Nesting: inline tokens nest arbitrarily inside bold/em/link text in both
> directions. Lists support one level of nesting: a ``<ul>``/``<ol>`` nested inside
> an ``<li>`` renders as a 2-space-indented sub-item (``  - `` or ``  N. `` per its
> own tag); a list nested inside ONE OF THOSE (three or more levels deep in the
> source) collapses into that same single sub-level. ``<ul>`` and ``<ol>`` mix
> freely at any level. A list item containing more than one block — GW's real
> ``<li><p>…</p><p>…</p></li>`` "loose" shape, or a paragraph followed by an evidence
> embed — renders as a blank-line-separated, indented continuation of the SAME item
> (CommonMark's own loose-list-item syntax).
>
> An item with exactly one paragraph (with or without a nested list) still renders
> bare, with no ``<p>`` wrapping, as before. This multi-block support is one level
> only — a nested (2nd-level) list item with multiple blocks of its own is outside
> this vocabulary.

Headings (`# `–`###### `) are accepted **only** when the converter is called with
`headings=True` — narrative sections and notes (§3), never a finding section (§2).
Tables, raw HTML blocks, blockquotes, fenced/indented code blocks, thematic breaks,
and link-reference definitions are never accepted anywhere in Ghostwriter-bound text,
in either direction.

Canonical normalizations (also quoted verbatim — these are never a validation
failure; `on_loss` reports them when the converter is invoked in that mode, but
`grison validate` does not care about them, since they never change meaning):

> 1. **Adjacent same-tag inline elements merge.**
> 2. **Adjacent same-tag top-level lists merge.**
> 3. **Whitespace moves out of ``<strong>``/``<em>`` boundaries.**
> 4. **Whitespace-only (or empty) ``<strong>``/``<em>``/``<code>`` is dropped.**
> 5. **A list item's content is always ``<p>``-wrapped on push.**
> 6. **A list nested more than one level deep collapses into that one sub-level.**

See `grison/markdown/converter.py`'s module docstring for the full text of each,
including the D1/D9 embed and cross-reference forms and D10's template-delimiter
escaping — those forms are what §5's `REF-…` rules validate the *reference*
correctness of; the converter itself validates their *shape*.

---

## 7. Index consistency (`IDX-…`)

These checks are purely static — comparing `.grison/index.json` against the
documents actually on disk. They never try to detect a rename or a move (that's the
sync engine's job, with network access); an indexed path with no file at that
location any more is not itself a validator failure.

- `IDX-001` — `.grison/index.json` is missing required top-level structure (no
  `records` key, `records` not an object), or an entry is malformed (unknown `kind`,
  non-integer `id`, an unexpected field, a non-POSIX-relative path, the same identity
  indexed under two different paths).
- `IDX-002` — an indexed path's recorded `kind` disagrees with what the path's own
  shape implies (e.g. `findings/library/x.md` indexed as `bs.page`). A path under a
  local-only tree (`findings/inbox/…` or `methodology/checklists/…`, §1.4) appearing
  in the index AT ALL is always `IDX-002` — neither tree corresponds to any real
  remote record, so any indexed kind there is a mismatch by construction.
- `IDX-003` — a document (finding, evidence file, narrative section, note) sits
  inside a `findings/reports/<dir>/` whose directory itself is not indexed as
  `gw.report`. grison never creates report directories by hand — an indexed report
  directory is the only legitimate source of one.
- `IDX-004` — an indexed entry under a report's `evidence/` folder is not kind
  `gw.evidence`, or one under a book's `images/` folder is not kind `bs.image`.

---

## 8. Banned text (`TXT-…`)

### 8.1 Built-in phrase list (`TXT-001`)

A small, narrow list of process-narration / assistant-chatter / tool-internals
phrases (`grison/validator/terms.py`'s `BANNED_PHRASES`) — deliberately short, since
pentest report prose legitimately discusses YAML, frontmatter, sync state, merge
bases, and similar vocabulary, none of which is banned. Matched case-insensitively,
whole-phrase, against every document body. Each entry is intended to catch an
assistant talking about *its own process*, e.g. "As an AI language model, I..." or "I
have updated the finding as requested" — never ordinary security-report language.

### 8.2 Per-workspace confidential terms (`TXT-002`)

`.grison/terms.txt` (private, never tracked): one term per line, `#` starts a
comment, blank lines ignored.

```
# client's real legal name — only allowed inside their own report directory
Acme Corporation => findings/reports/14-acme
# never allowed anywhere
internal-codename-orca
```

`term => allowed/path/prefix` allows that term only under one workspace-relative path
prefix (normally one report directory); a bare `term` line is never allowed anywhere.
Matching is case-insensitive, whole-word, Unicode-aware. A hit outside its allowed
prefix is `TXT-002` — reported as the line number and a **masked** form (first
character, then `*` for the rest) so the term itself is never echoed into output that
could be committed or copied elsewhere.

---

## 9. The `grison validate` command

```
grison validate [PATHS…] [--json] [--deleted-ok]
```

Offline, no credentials, no network. Quiet on success. Without `--json`, one line per
failure: `path[:line]: RULE-ID message — fix`. With `--json`, a stable array of
`{rule_id, path, line, message, fix}` objects instead.

### 9.1 Workspace discovery

`grison validate` works from any directory inside the workspace, the same way `git`
finds `.git/`: it walks up from the current directory to the nearest ancestor
containing `.grison/` and uses that as the workspace root. If no ancestor has one, it
exits `2` with a plain message — there is no workspace to validate. Every `PATHS`
argument is resolved relative to the CURRENT directory (not the workspace root) and
then expressed relative to the root that was found — `cd findings/reports/globex &&
grison validate broken-auth.md` validates that one file exactly as
`grison validate findings/reports/globex/broken-auth.md` would from the workspace
root, and `cd findings/reports/globex && grison validate` (no `PATHS`) validates the
**whole workspace**, not just that directory — omitting `PATHS` always means
everything, regardless of where the command was run from.

### 9.2 What a `PATHS` entry means

**A `PATHS` entry never silently matches nothing.** Every entry resolves to a real
scope or the command fails — there is no way to spell a path that makes
`grison validate` report a false "clean":

- A path **equal to the workspace root** (`.`, `./`, or the workspace root's absolute
  path) means the whole workspace — identical to omitting `PATHS` entirely.
- A path naming a **directory** means everything under it, plus the cross-file rules
  that touch it (its evidence-or-image stems and captions, its mirrors, index
  consistency) — see §9.3 for exactly what each directory expands to.
- A path naming a **file** means that document's own failures, plus any cross-file
  failure that names it — never a sibling file's own-only failures. Concretely: every
  rule this validator has, including the cross-document ones (`REF-003` stem
  collisions, `REF-004` caption conflicts, `IDX-003`, `WS-009`/`WS-010`, `WIKI-007`,
  …), already files each involved document's failure under THAT document's own
  `path` — never one shared failure for a whole directory. So "narrow to file X" is
  exactly "keep only failures whose own `path` is X": no extra cross-referencing
  logic, just a filter over the containing report/book/checklist directory's full
  failure list (§9.3).
- A path that **does not exist** is a hard failure (exit `2`, `error: no such path:
  …`) — unless `--deleted-ok` is given (§9.4).
- A path **outside the workspace** is a hard failure (exit `2`,
  `error: path is outside the workspace: …`).
- A path **inside `.grison/`**, or anywhere else that isn't a validated location (a
  `.git/` path, a stray top-level file, anything not under `findings/` or
  `methodology/`) is a hard failure (exit `2`, naming the path and saying it is not a
  workspace document).

The library function behind the CLI, `validate_workspace(root, *, paths=None,
deleted_ok=False)`, has exactly these semantics — it raises
`grison.validator.ValidationScopeError` (a `GrisonError`) for the last three cases
above, rather than returning an empty list, since the sync engine and the post-edit
hook call it directly and must never mistake "nothing was checked" for "nothing is
wrong."

### 9.3 Directory expansion

| directory | expands to |
|---|---|
| the workspace root (`.`) | everything |
| `findings/` | `findings/library/`, `findings/reports/` (every report dir), `findings/inbox/`, plus the `findings/`-level and `findings/library/`-level layout checks |
| `findings/library/` | every library finding, plus its layout check |
| `findings/reports/` | every report directory |
| `findings/reports/<dir>/` | that one report directory (findings, narrative, notes, evidence, mirrors) |
| `findings/inbox/` | every inbox finding, plus its layout check |
| `methodology/` | `methodology/library/` (every book), `methodology/checklists/` (every engagement), plus the `methodology/`-level layout check and `.shelves/` |
| `methodology/library/` | every book, plus `.shelves/` |
| `methodology/library/<book>/` | that one book (pages, chapters, images, mirrors) |
| `methodology/library/.shelves/` | every shelf mirror |
| `methodology/checklists/` | every checklist engagement |
| `methodology/checklists/<engagement>/` | that one engagement (same shape as a book) |

This table is exactly why the property "the union of validating `findings/` and
`methodology/` separately equals validating the whole workspace" holds — both are
built from the same discovery the whole-workspace scan uses, just scoped narrower.
The same property holds one level down: validating every file inside
`findings/reports/<dir>/` (or `methodology/library/<book>/`) one at a time and
taking the union of the results equals validating that directory in one call — a
**directory** argument always returns the unnarrowed, whole-directory result (today's
behaviour, unchanged); only a **file** argument narrows (§9.2).

### 9.4 `--deleted-ok`

The post-edit hook runs after a file is saved OR deleted; a deleted file's path no
longer exists, but the hook still needs to run the cross-file rules for the directory
it was in (e.g. an evidence file's deletion can resolve a stale `REF-002`, or leave a
caption conflict behind). `--deleted-ok` (`deleted_ok=True` in the library function)
changes ONLY the "path does not exist" case: instead of failing, a missing path
resolves to its **parent directory's** scope (§9.3) — the same as if the parent
directory itself had been passed. If the parent directory doesn't exist either, it
still fails. This does not change any other case: an existing path that isn't a
validated location, or one outside the workspace, still fails regardless of
`--deleted-ok`.

### 9.5 Exit code

Three-way, so a pre-commit hook or CI step can tell "your documents are invalid"
apart from "validate itself is broken":

| code | meaning |
|---|---|
| `0` | the workspace is clean — nothing to fix |
| `1` | the validator ran to completion and found one or more real document problems (listed) |
| `2` | the validator itself could not run — no workspace found, a `PATHS` entry that doesn't resolve to a real location (§9.2), or an unexpected internal failure; nothing about the documents was determined either way |

Exit `2` is deliberately distinct from `1`: a caller must never mistake "grison could
not evaluate your documents" for "your documents are fine," and must never mistake it
for "your documents are broken" either — it means neither answer is known.

---

## 10. Scaffolded files (brief D11/D12/D13)

Every one of these is generated from the same definitions this document and
`grison/validator/registry.py` describe (`grison/scaffold/`), so it can never say
something the validator doesn't also check. `grison scaffold [--force]` (re)generates
all of them on demand; `grison sync`/`grison parse` do the same automatically so a
first run in an empty directory yields a complete, self-contained workspace.

| path | owner | regenerated |
|---|---|---|
| `.grison/SPEC.md` | grison | every scaffold run — this is a copy of this exact file, carries no authored content, so it is always kept current |
| `.grison/templates/*.md` | grison, then the operator | once — grison never overwrites a template that already exists (an operator may have customized it) |
| `.grison/terms.txt` | the operator (private, D12) | once — grison never overwrites an existing one (it may hold real confidential terms) |
| `CLAUDE.md` | grison, then the operator | self-healing only while unmodified since the last generation AND stale (an older grison/spec version's marker) — a hand-edited copy is left alone; `grison scaffold --force` always regenerates it |
| `.claude/settings.json` | grison + the operator, merged | grison's own deny rules/hook entry are self-healing on every run (a guardrail an edit can't silently disable); every other key is passed through untouched; `--force` additionally prunes and re-adds grison's own entries (handles a wording change across versions) |
| `.gitignore` (workspace root) | grison + the operator, merged | grison's `*.remote.*` block is self-healing, same as `.claude/settings.json`; every other line is passed through untouched |
| `.git/hooks/pre-commit` | grison, when this is a git repo | grison updates its own previously-installed hook; a foreign (non-grison) existing hook is never overwritten — the exact text to add by hand is printed instead |

**`.grison/` is not uniformly private.** `.grison/SPEC.md` and `.grison/templates/`
exist precisely so an agent can read the spec and copy a template — the scaffolded
`.claude/settings.json` denies READ only for the genuinely private entries
(`grison.manifest.PRIVATE_ENTRIES`: `env`, `state/`, `snapshots/`, `lock`,
`terms.txt`) and denies EDIT/WRITE for all of `.grison/` uniformly (nothing under it
is ever agent-writable, tracked files included). The two lists are the same tuples
`.grison/.gitignore`'s allow-list is generated from
(`grison.manifest.TRACKED_ENTRIES`/`PRIVATE_ENTRIES`), so "readable" and "tracked"
can never drift apart.

`.grison/SPEC.md`'s own integrity (a hand-edited or missing copy) is not currently a
`WS-…` validator rule — unlike every other read-only mirror (§1.9's `WS-009`), the
copy has no dedicated rule id yet; this is a known, intentionally deferred gap. The
read side is already in place: `grison.scaffold.orchestrate.scaffold_workspace`
records a digest in `.grison/state/mirrors.json` for `.grison/SPEC.md` (every run),
each `.grison/templates/*.md` (the run that first writes it), and `CLAUDE.md` (every
run that (re)writes it) via `grison.validator.mirrors.record_digest` — the same
mechanism `WS-009` already reads. Only the rule itself (comparing current content
against the recorded digest, the `WS-009` pattern exactly) is missing, since it needs
`grison/validator/core.py`.

---

## Appendix: full rule table

| id | summary |
|---|---|
| WS-001 | a file or directory name under findings/ or methodology/ is invalid |
| WS-002 | an unrecognized file or directory sits under findings/ (includes findings/inbox/ — validated, not synced) |
| WS-003 | an unrecognized file or directory sits under methodology/ (includes methodology/checklists/ — validated, not synced) |
| WS-004 | **RETIRED** — see §1.3; superseded by WS-001 + IDX-003 |
| WS-005 | the workspace format is older than this grison supports |
| WS-006 | the workspace format is newer than this grison supports |
| WS-007 | .grison/manifest.yml is missing or malformed |
| WS-008 | a private .grison/ path is tracked, or a tracked one is git-ignored |
| WS-009 | a read-only mirror file's content differs from what grison last generated |
| WS-010 | a read-only mirror file is not valid for its type |
| FND-001 | an unrecognized frontmatter field is present on a finding |
| FND-002 | a required frontmatter field is missing on a finding |
| FND-003 | severity is not one of informational/low/medium/high/critical |
| FND-004 | finding_type is not a recognized value |
| FND-005 | cvss.vector is not a well-formed CVSS 3.0/3.1 base vector |
| FND-006 | a cwe entry is not a known CWE id |
| FND-007 | tags is not a list of distinct, non-empty, untrimmed-whitespace strings |
| FND-008 | affected_entities is set on a library finding |
| FND-009 | a required '##' section is missing from the finding body |
| FND-010 | a '##' heading in the finding body is not a recognized section name |
| FND-011 | a '##' section heading repeats in the finding body |
| FND-012 | the finding body's sections are not in the fixed order |
| FND-013 | the finding body has no '# {title}' heading, or it is blank |
| FND-014 | a finding section's markdown does not convert to Ghostwriter HTML |
| FND-015 | severity does not match the CVSS v3 band of cvss.vector's score |
| FND-016 | text sits outside every recognized section in a finding body |
| FND-017 | the finding document's frontmatter is missing or not valid YAML |
| REP-001 | a narrative section's or note's markdown does not convert to Ghostwriter HTML |
| REP-002 | a notes/ file's frontmatter shape does not match whether it is indexed |
| WIKI-001 | an unrecognized frontmatter field is present on a wiki page |
| WIKI-002 | a wiki page's title is missing or blank |
| WIKI-003 | a wiki page's priority is not an integer |
| WIKI-004 | a wiki page's tags is not a list of distinct, non-empty, untrimmed-whitespace strings |
| WIKI-005 | a wiki page body contains a real HTML tag outside a code span/fence |
| WIKI-006 | a wiki page link uses a scheme other than http/https/mailto |
| WIKI-007 | a wiki page's internal BookStack link does not resolve |
| WIKI-008 | a wiki page body contains a zero-width or bidi control character |
| WIKI-009 | a wiki page file uses CRLF line endings |
| WIKI-010 | a wiki page body line has trailing whitespace |
| WIKI-011 | a wiki page file does not end with exactly one newline |
| WIKI-012 | a wiki page body skips a heading level |
| WIKI-013 | a wiki page body's first heading repeats the frontmatter title |
| WIKI-014 | the wiki page document's frontmatter is missing or not valid YAML |
| REF-001 | an image is not alone in its own block |
| REF-002 | an image or cross-reference path does not resolve to a file |
| REF-003 | two files in the same evidence/ or images/ folder share a stem |
| REF-004 | two references to the same file give it different non-empty captions |
| REF-005 | a library or inbox finding contains an image line |
| REF-006 | a cross-reference link's target is not a resolvable evidence path |
| REF-007 | a wiki image uses the wrong path spelling for its location |
| TXT-001 | a built-in banned phrase appears in a document body |
| TXT-002 | a per-workspace confidential term appears outside its allowed path |
| IDX-001 | .grison/index.json is missing required structure or malformed |
| IDX-002 | an indexed entry's kind does not match its path shape |
| IDX-003 | a document sits inside a findings/reports/ directory that is not itself indexed |
| IDX-004 | an indexed evidence/image entry's kind does not match its folder |
