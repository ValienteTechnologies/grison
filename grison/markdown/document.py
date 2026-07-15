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


def _strip_state(dumped: dict) -> None:
    """Remove volatile sync-state and derived values from the frontmatter dict — they live
    in ``.grison/state/`` (base + evidence hash/meta/basename) or are recomputed on read
    (``cvss.score`` from the vector, ``grison.tier`` from ``gw.table``). ``grison.kind`` is a
    constant nothing branches on. The file keeps only content + identity (``grison.gw`` and
    each ``evidence[].gw.id``); a git checkout of it can never revert grison's merge base."""
    grison = dumped.get("grison")
    if isinstance(grison, dict):
        for k in ("synced", "kind", "tier"):
            grison.pop(k, None)
    cvss = dumped.get("cvss")
    if isinstance(cvss, dict):
        cvss.pop("score", None)
    for ev in dumped.get("evidence") or []:
        gw = ev.get("gw") if isinstance(ev, dict) else None
        if isinstance(gw, dict):
            for k in ("hash", "meta", "basename"):
                gw.pop(k, None)


def finding_to_markdown(f: Finding) -> str:
    """Render a Finding as its markdown document (frontmatter + title + sections).

    Only content + identity are written; the merge base, per-image bookkeeping, and derived
    values are excluded (see :func:`_strip_state`) — they belong to the private state store,
    not the git-tracked file."""
    dumped = f.model_dump(mode="json", exclude_none=True)
    _strip_state(dumped)
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


def _derive_tier(grison: object, tier: str | None) -> str | None:
    """``grison.tier`` no longer lives in the file — recover it for validation. Prefer an
    explicit frontmatter value (back-compat with pre-refactor files), then the caller's
    location-derived hint, then derive from the identity ``gw.table`` (``finding`` → library,
    ``reportedFinding`` → instance). ``None`` only when a table-less file is parsed with no
    hint, which surfaces as a normal validation error."""
    if not isinstance(grison, dict):
        return tier
    if grison.get("tier") is not None:
        return grison["tier"]
    table = (grison.get("gw") or {}).get("table")
    if table is not None:
        return "library" if table == "finding" else "instance"
    return tier


def markdown_to_finding(text: str, *, tier: str | None = None) -> Finding:
    """Parse a markdown document back into a validated Finding.

    ``tier`` is derived from the in-file ``gw.table`` (it is no longer stored); ``tier=`` lets
    the sync engine supply the location-derived value for a table-less file it is scanning.
    The merge base and per-image bookkeeping are absent here — :func:`grison.state.hydrate_finding`
    fills them from the state store after parse; ``cvss.score`` recomputes from the vector."""
    frontmatter, body = _split_frontmatter(text)
    title, sections = _parse_body(body)

    unknown = set(sections) - set(_HEADER_TO_FIELD)
    if unknown:
        raise DocumentError(f"unknown section(s): {', '.join(sorted(unknown))}")

    data = dict(frontmatter)
    data["title"] = title
    for header, field in _SECTIONS:
        data[field] = sections.get(header, "")
    grison = data.get("grison")
    derived = _derive_tier(grison, tier)
    if isinstance(grison, dict) and derived is not None:
        grison["tier"] = derived
    return Finding.model_validate(data)


def extract_gw_identity(text: str) -> tuple[str, int] | None:
    """Best-effort ``(gw.table, gw.id)`` from a document's raw frontmatter, used when
    the document fails full parse/validation (corrupt-file guard, gw-pull F1) — a
    broken body or an invalid field must not stop the sync engine from still knowing
    which remote record this file claims, so the remote-only pull loop doesn't
    re-materialize it from scratch over the broken file.

    Tolerates every failure shape short of a clean ``(table, id)`` pair: no
    frontmatter fence, unterminated fence, invalid YAML, non-mapping frontmatter,
    missing/malformed ``grison.gw`` — all return ``None`` rather than raising.
    """
    try:
        frontmatter, _body = _split_frontmatter(text)
    except DocumentError:
        return None
    gw = frontmatter.get("grison")
    if not isinstance(gw, dict):
        return None
    gw = gw.get("gw")
    if not isinstance(gw, dict):
        return None
    table, gw_id = gw.get("table"), gw.get("id")
    if table not in ("finding", "reportedFinding") or not isinstance(gw_id, int):
        return None
    return table, gw_id
