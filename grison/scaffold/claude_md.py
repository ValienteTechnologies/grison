"""``CLAUDE.md`` (workspace root) — generated agent instructions (brief D11).

Every fact that could drift from the validator is pulled live from the same
definitions the validator enforces — :mod:`grison.formats` (frontmatter fields, body
section order) and :mod:`grison.model.enums` (allowed ``severity``/``finding_type``
values) — instead of being retyped by hand. The full per-rule detail
(:mod:`grison.validator.registry`'s ids/summaries/fixes) is deliberately NOT embedded
here — every ``grison validate`` failure line already carries its own rule id,
message, and fix, and the complete rule text lives in the workspace's own
``.grison/SPEC.md`` (this file just says so); repeating all of it here would make this
file long without making an agent that already sees a failure line any better off.

This module says nothing about how grison stores, hashes, or reconciles anything —
only what an agent may write, where, and how to check it — per the brief's content
rule: no mention of state, hashes, merge bases, or index contents to an agent working
in the workspace.
"""

from __future__ import annotations

from enum import Enum

from grison import __version__
from grison.formats.finding import SECTIONS
from grison.model.enums import FindingType, Severity

MARKER_PREFIX = "<!-- grison-generated CLAUDE.md"
SPEC_FORMAT = 2  # the workspace format this file's content describes (brief D13)

# Written as a plain string, not inline in the f-string below, purely so the literal
# template-syntax examples (real curly braces) don't have to be hand-doubled to
# survive f-string interpolation.
_PAYLOADS_SECTION = """\
## Payloads that look like template syntax

A finding legitimately quotes a template-injection payload the way the target
reflected it — `{7*7}`, `{{7*7}}`, `{% debug %}`, `${{ ... }}`, or similar Jinja/
Handlebars/Go-template syntax. Write it literally, exactly as it was sent — in a code
span when it is code. Do not escape, mangle, or "defuse" it; grison escapes it
correctly for you when syncing to Ghostwriter.\
"""


def marker_line(*, grison_version: str | None = None) -> str:
    version = grison_version if grison_version is not None else __version__
    return (
        f"{MARKER_PREFIX} — grison {version}, spec format {SPEC_FORMAT}; "
        "regenerate with `grison scaffold --force`; never hand-edit -->"
    )


def _values(cls: type[Enum]) -> str:
    return ", ".join(str(m.value) for m in cls)


def build_claude_md(*, grison_version: str | None = None) -> str:
    """The full, deterministic ``CLAUDE.md`` text."""
    sections_order = ", ".join(f"`## {h}`" for h, _ in SECTIONS)
    severities = _values(Severity)
    finding_types = _values(FindingType)

    return f"""\
{marker_line(grison_version=grison_version)}

# Working in this grison workspace

This directory is a grison workspace: markdown here IS the data. Editing a file and
running `grison sync` (an owner, not you) is how it reaches Ghostwriter/BookStack.

## Layout

```
findings/
  library/*.md
  reports/<dir>/
    <name>.md
    evidence/
    narrative/<field>.md
    notes/*.md
    project.md  .report.yml
  inbox/*.md
methodology/
  library/<book>/[<chapter>/]*.md
  library/<book>/images/*
  checklists/<engagement>/[<chapter>/]*.md
```

- `findings/library/` — reusable findings, not tied to a report.
- `findings/reports/<dir>/` — one directory per existing report. grison never creates
  report directories; only write findings directly inside one that already exists.
- `findings/reports/<dir>/narrative/<field>.md` — one file per report narrative field.
- `findings/reports/<dir>/notes/*.md` — project notes (see below).
- `findings/inbox/` — triage area for `grison parse` output. Local-only, never synced,
  but every rule below still applies to it.
- `methodology/library/<book>/[<chapter>/]*.md` — wiki pages. A page's book and
  chapter are decided by which directory it sits in — there is no `book`/`chapter`
  field.
- `methodology/checklists/<engagement>/` — a working copy of a book, local-only, never
  synced, still fully validated.

## Naming

A new file or directory you create may be named anything matching
`[a-z0-9][a-z0-9._-]*` (lowercase, digit/letter start) — except a file directly inside
`evidence/`/`images/`, kept verbatim (uppercase/non-ASCII fine; no `/`, no leading dot,
not `<name>.remote.<ext>`). Grison never renames what it already named on pull.

## `.grison/`

`.grison/SPEC.md` is the full format spec — read it when a rule is unclear.
`.grison/templates/` has a starting file for each document type — copy one when you
create a new document. Everything else in `.grison/` is grison's: never read it, and
never write anywhere in `.grison/`.

## Finding frontmatter

Starting point: `.grison/templates/finding-library.md` (library) or
`.grison/templates/finding-reported.md` (inside a report).

| field | required | values |
|---|---|---|
| `severity` | yes | {severities} |
| `finding_type` | yes | {finding_types} |
| `cvss.vector` | no | a CVSS 3.0/3.1 vector, e.g. `CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` |
| `cwe` | no | a list of CWE ids, e.g. `CWE-79` |
| `tags` | no | a list of strings, no duplicates, no surrounding whitespace |
| `affected_entities` | no | free text — report directories only, never `findings/library/` |

No other frontmatter key is allowed. Body: exactly one `# {{title}}` line, then all
five of {sections_order}, in that order, each exactly once. Severity must agree with
the CVSS band when both are set (this does not apply in `findings/inbox/`, since a
scanner's own rating and a naive CVSS band routinely disagree before you've triaged
the finding).

## Wiki page frontmatter (`methodology/library/<book>/[<chapter>/]*.md`)

Starting point: `.grison/templates/wiki-page.md`.

| field | required | values |
|---|---|---|
| `title` | yes | non-blank string |
| `priority` | no | integer (BookStack sort order) |
| `tags` | no | a list of strings, same rule as a finding's `tags` |

No other frontmatter key. The body is markdown BookStack renders verbatim: no raw
HTML tags (a placeholder like `<domain>` is fine — it isn't a real tag name), only
`http:`/`https:`/`mailto:` links, no heading-level skips, and the first heading must
not repeat the title.

## Notes (`findings/reports/<dir>/notes/*.md`)

Starting point: `.grison/templates/project-note.md`. A NEW note you are creating has
no frontmatter fence at all — just write the body. A note that already has
`author`/`timestamp` frontmatter is a mirror of an existing Ghostwriter note; never
add a frontmatter fence to a new note, and never edit a mirrored one's frontmatter.

## Evidence and wiki images

The only way to attach an image is a markdown image line, alone in its own block (or
its own block inside one list item): `![caption](evidence/file.png "optional
description")` inside a report file/narrative/note, or `![caption](images/file.png)`
(`../images/file.png` from inside a chapter) on a wiki page. The path must name a real
file already sitting in that `evidence/`/`images/` folder. A plain link,
`[text](evidence/file.png)`, is a cross-reference ("see Figure N"), not an embed.
Neither form is allowed in a library or inbox finding — there is no report yet to hold
evidence for. Removing an image line never deletes the file itself.

{_PAYLOADS_SECTION}

## Read-only files

`.report.yml`, `project.md`, a *mirrored* note, `.book.yml`, `.chapter.yml`, and
`.shelves/*.yml` are regenerated every sync — read them for context, never edit them.
`grison validate` rejects a hand-edit to any of these on sight.

## Commands

- `grison validate [PATH]` — check the workspace (or one file/directory), offline.
- `grison status` — a whole-workspace overview, offline.
- `grison parse <file>` — turn scanner output into `findings/inbox/` markdown, offline.

Never run `grison sync`, `grison sync --force-local`/`--force-remote`, or
`grison undo` — all owner-only; your `.claude/settings.json` refuses them.

**Run `grison validate` before you finish. Fix every failure it reports. Never work
around a failure** (renaming around a check, stripping content just to pass, etc.) —
fix the actual document instead. A failure line looks like
`path:line: RULE-ID message — fix`; the fix text is the instruction.
`.grison/SPEC.md` has the full rule list if you need more context.

## Scope discipline

Work only within the scope entries listed in a report's `project.md`. An entry marked
`EXCLUDED` is off-limits: do not create findings, evidence, or narrative referencing
it.
"""
