"""Serialize a :class:`~grison.model.Finding` to/from its markdown document.

The on-disk shape is YAML frontmatter (the structured fields) + ``# {title}`` +
five fixed ``## `` sections whose bodies are markdown. Those ``##`` headers are
grison *structure* — they map to Ghostwriter's separate fields — not field
content. Round-trip: ``markdown_to_finding(finding_to_markdown(f)) == f``.
"""

from __future__ import annotations

import yaml

from grison.model import Finding

# (section header in the document, model field). Fixed order, always all five.
_SECTIONS: list[tuple[str, str]] = [
    ("Description", "description"),
    ("Impact", "impact"),
    ("Mitigation", "mitigation"),
    ("Replication Steps", "replication_steps"),
    ("References", "references"),
]
_HEADER_TO_FIELD = {h: f for h, f in _SECTIONS}
_BODY_FIELDS = {f for _, f in _SECTIONS}


class DocumentError(ValueError):
    """A markdown document that can't be parsed into a Finding (bad frontmatter/structure)."""


def _prune_empty(obj: object) -> object:
    """Drop None / empty-list / empty-dict entries so frontmatter stays tidy."""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            pruned = _prune_empty(v)
            if pruned is None or pruned == [] or pruned == {}:
                continue
            out[k] = pruned
        return out
    if isinstance(obj, list):
        return [_prune_empty(x) for x in obj]
    return obj


def finding_to_markdown(f: Finding) -> str:
    """Render a Finding as its markdown document (frontmatter + title + sections)."""
    dumped = f.model_dump(mode="json", exclude_none=True)
    title = dumped.pop("title")
    bodies = {field: dumped.pop(field, "") or "" for _, field in _SECTIONS}
    frontmatter = _prune_empty(dumped)

    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    parts = ["---", fm_yaml, "---", "", f"# {title}", ""]
    for header, field in _SECTIONS:
        parts.append(f"## {header}")
        content = bodies[field].strip()
        if content:
            parts.extend(["", content])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise DocumentError("document has no YAML frontmatter (must start with '---')")
    # Split on the closing fence: lines[0] is '---', find the next '---' line.
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            break
    else:
        raise DocumentError("unterminated YAML frontmatter (no closing '---')")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise DocumentError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise DocumentError("frontmatter is not a mapping")
    return data, body


def _parse_body(body: str) -> tuple[str, dict[str, str]]:
    title: str | None = None
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buf).strip()

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            current = line[3:].strip()
            buf = []
        elif line.startswith("# ") and title is None and current is None:
            title = line[2:].strip()
        else:
            buf.append(line)
    flush()

    if title is None:
        raise DocumentError("document body has no '# {title}' heading")
    return title, sections


def markdown_to_finding(text: str) -> Finding:
    """Parse a markdown document back into a validated Finding."""
    frontmatter, body = _split_frontmatter(text)
    title, sections = _parse_body(body)

    unknown = set(sections) - set(_HEADER_TO_FIELD)
    if unknown:
        raise DocumentError(f"unknown section(s): {', '.join(sorted(unknown))}")

    data = dict(frontmatter)
    data["title"] = title
    for header, field in _SECTIONS:
        data[field] = sections.get(header, "")
    return Finding.model_validate(data)
