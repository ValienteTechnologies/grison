"""``.grison/templates/`` — starting points for a library finding, a reported finding,
a wiki page, and a project note.

Every value that could drift from the validator (allowed ``severity``/``finding_type``
values, the five section headers and their fixed order) is pulled live from
:mod:`grison.formats.finding` and :mod:`grison.model.enums` rather than hand-typed, so a
future enum/section change can never leave a stale template behind silently — see
``tests/test_scaffold_templates.py``, which also proves each template, copied to its
proper place in a real workspace, validates clean.

Frontmatter fields an author might want but doesn't have to fill in yet (``tags``,
``cvss``, ``priority``, ``affected_entities``) are shown as YAML comments (which
pydantic's ``extra="forbid"`` never sees, since they're not real keys) rather than
live, so a fresh copy of the template is valid-by-construction without any edits at
all — an agent starts from working content and grows it, instead of starting from
something that fails validation until specific placeholders are replaced.
"""

from __future__ import annotations

from grison.formats.finding import SECTIONS
from grison.model.enums import FindingType, Severity

TEMPLATES_RELATIVE_DIR = ".grison/templates"

FINDING_LIBRARY_NAME = "finding-library.md"
FINDING_REPORTED_NAME = "finding-reported.md"
WIKI_PAGE_NAME = "wiki-page.md"
PROJECT_NOTE_NAME = "project-note.md"

_PLACEHOLDER_SECTIONS: dict[str, str] = {
    "description": "Describe the vulnerability: what it is, and where it was found.",
    "impact": "Describe what an attacker could accomplish by exploiting this.",
    "mitigation": "Describe how to fix or mitigate the issue.",
    "replication_steps": "1. Describe the first step to reproduce the issue.\n"
    "2. Describe the next step.",
    "references": "- https://example.com/reference",
}


def _enum_list(values: tuple[str, ...]) -> str:
    return ", ".join(values)


_SEVERITY_VALUES = tuple(s.value for s in Severity)
_FINDING_TYPE_VALUES = tuple(t.value for t in FindingType)


def _finding_body(title: str) -> str:
    lines = [f"# {title}", ""]
    for header, field in SECTIONS:
        lines.append(f"## {header}")
        lines.append("")
        lines.append(_PLACEHOLDER_SECTIONS[field])
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def finding_library_template() -> str:
    """``findings/library/<name>.md`` — a reusable, tier-``library`` finding."""
    frontmatter = (
        "---\n"
        f"# severity: one of {_enum_list(_SEVERITY_VALUES)}\n"
        "severity: informational\n"
        f"# finding_type: one of {_enum_list(_FINDING_TYPE_VALUES)}\n"
        "finding_type: web\n"
        "# cvss:\n"
        "#   vector: CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H\n"
        "# cwe:\n"
        "#   - CWE-79\n"
        "# tags:\n"
        "#   - example-tag\n"
        "---\n"
    )
    return frontmatter + _finding_body("Untitled finding")


def finding_reported_template() -> str:
    """``findings/reports/<dir>/<name>.md`` — a tier-``instance`` finding, placed
    directly inside an indexed report directory. Only this tier may also set
    ``affected_entities`` (shown commented, like every other optional field)."""
    frontmatter = (
        "---\n"
        f"# severity: one of {_enum_list(_SEVERITY_VALUES)}\n"
        "severity: informational\n"
        f"# finding_type: one of {_enum_list(_FINDING_TYPE_VALUES)}\n"
        "finding_type: web\n"
        "# cvss:\n"
        "#   vector: CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H\n"
        "# cwe:\n"
        "#   - CWE-79\n"
        "# tags:\n"
        "#   - example-tag\n"
        "# affected_entities: |\n"
        "#   https://app.example.com/path\n"
        "---\n"
    )
    return frontmatter + _finding_body("Untitled finding")


def wiki_page_template() -> str:
    """``methodology/library/<book>/[<chapter>/]<name>.md`` — a BookStack page. Its
    book/chapter come from where you place the file, never from frontmatter (D4)."""
    return (
        "---\n"
        "title: Untitled page\n"
        "# priority: integer BookStack sort order (optional; omit to let BookStack decide)\n"
        "# tags:\n"
        "#   - example-tag\n"
        "---\n"
        "Write the page content here — BookStack stores and renders this markdown "
        "verbatim.\n"
    )


def project_note_template() -> str:
    """``findings/reports/<dir>/notes/<name>.md`` — a NEW local note (never a
    frontmatter fence: that shape is reserved for an already-mirrored note grison
    itself regenerates — see ``docs/workspace-format.md`` §3.2)."""
    return (
        "Write a new note for the project here — grison pushes it as a new "
        "Ghostwriter project note on the next sync.\n"
    )


def all_templates() -> dict[str, str]:
    """Every template file's name -> content, in the order they're documented."""
    return {
        FINDING_LIBRARY_NAME: finding_library_template(),
        FINDING_REPORTED_NAME: finding_reported_template(),
        WIKI_PAGE_NAME: wiki_page_template(),
        PROJECT_NOTE_NAME: project_note_template(),
    }
