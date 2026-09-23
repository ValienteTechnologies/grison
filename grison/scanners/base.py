from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from grison.scanners.ir import ScanFinding, Severity
from grison.scanners.ir.severity import SEVERITY_ORDER
from grison.scanners.ir.severity import max_severity as _max_severity

_WHITESPACE_RUN = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Collapse any run of whitespace (including an embedded newline plus XML
    indentation, as OpenVAS NVT text carries) to a single space, and strip.

    Applied to finding titles only: the finding format renders a title as a single
    markdown heading line, so a raw multi-line title would otherwise wrap into body
    text outside its section.
    """
    return _WHITESPACE_RUN.sub(" ", text).strip()


def refs_to_html(refs: Sequence[str | tuple[str, str]]) -> str:
    """Render a list of references as an HTML ``<ul>``, once, for every parser.

    Each item is either a plain string (rendered as a link if it looks like an
    http(s) URL, else as plain text) or an ``(anchor_text, url)`` pair (rendered as
    a link with that anchor text — Burp's references arrive as existing ``<a>``
    tags whose label must survive). Both text and href are always ``html.escape``d.
    Returns ``""`` for an empty list, matching every parser's prior behaviour of
    omitting the field rather than emitting an empty ``<ul></ul>``.
    """
    if not refs:
        return ""

    items: list[str] = []
    for ref in refs:
        if isinstance(ref, tuple):
            text, url = ref
            label = html.escape(text.strip() or url)
            items.append(f'<li><a href="{html.escape(url, quote=True)}">{label}</a></li>')
        elif ref.startswith("http"):
            escaped = html.escape(ref)
            items.append(f'<li><a href="{html.escape(ref, quote=True)}">{escaped}</a></li>')
        else:
            items.append(f"<li>{html.escape(ref)}</li>")
    return f"<ul>{''.join(items)}</ul>"


# The reference shape every parser converges on: either a bare string (rendered
# as a link when it looks like a URL) or an (anchor_text, url) pair — the same
# type refs_to_html accepts. Kept as raw pieces (not pre-rendered HTML) so the
# Aggregator's "first occurrence wins" merge rule applies before rendering.
FindingReferences = list[str | tuple[str, str]]


@dataclass
class RawOccurrence:
    """One raw record yielded by a parser's document walk, before aggregation.

    ``key`` is the scanner-native id used to group occurrences of the same
    finding (Nessus plugin id, Burp/ZAP/Acunetix/Qualys/OpenVAS/SSLyze vuln id).

    The fields below ``affected_component`` mirror :class:`ScanFinding`'s own
    prose/metadata fields — only the first occurrence's values for a given key
    are kept by :class:`Aggregator` (matching every parser's prior "meta built
    once, on first sight" shape); later occurrences only contribute their
    ``affected_component`` and ``severity``. Explicit typed attributes (instead
    of a ``dict[str, Any]`` "fields" bag keyed by ad hoc strings) mean a typo in
    a field name is a mypy error, not a silent ``KeyError`` at runtime.
    """

    key: str
    title: str
    severity: Severity
    affected_component: str = ""  # "" contributes nothing (no component for this occurrence)
    cvss_vector: str = ""
    cwe: str = ""
    description: str = ""
    impact: str = ""
    mitigation: str = ""
    references: FindingReferences = field(default_factory=list)
    replication_steps: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class AggregatedRecord:
    """One merged finding, ready for a parser to turn into a :class:`ScanFinding`.

    Same typed prose/metadata fields as :class:`RawOccurrence` (see its
    docstring) — the aggregated record is a :class:`ScanFinding` under
    construction plus the merge state (``key`` for regrouping, ``affected_components``
    accumulated across occurrences).
    """

    key: str
    title: str
    severity: Severity
    affected_components: list[str]
    cvss_vector: str = ""
    cwe: str = ""
    description: str = ""
    impact: str = ""
    mitigation: str = ""
    references: FindingReferences = field(default_factory=list)
    replication_steps: str = ""
    tags: list[str] = field(default_factory=list)


class Aggregator:
    """Groups raw occurrences by their scanner-native id.

    Replaces the eight copy-pasted ``dict[str, dict]`` aggregation blocks that used
    to live in the individual parsers: extends ``affected_components`` without
    duplicates (first-seen order), merges severity by MAX across all occurrences of
    the same id, keeps the first occurrence's other fields, and collapses embedded
    whitespace in the title.
    """

    def __init__(self) -> None:
        self._order: list[str] = []
        self._records: dict[str, AggregatedRecord] = {}

    def add(self, occurrence: RawOccurrence) -> None:
        title = collapse_whitespace(occurrence.title)
        existing = self._records.get(occurrence.key)
        if existing is None:
            self._order.append(occurrence.key)
            self._records[occurrence.key] = AggregatedRecord(
                key=occurrence.key,
                title=title,
                severity=occurrence.severity,
                affected_components=(
                    [occurrence.affected_component] if occurrence.affected_component else []
                ),
                cvss_vector=occurrence.cvss_vector,
                cwe=occurrence.cwe,
                description=occurrence.description,
                impact=occurrence.impact,
                mitigation=occurrence.mitigation,
                references=occurrence.references,
                replication_steps=occurrence.replication_steps,
                tags=occurrence.tags,
            )
            return

        existing.severity = _max_severity(existing.severity, occurrence.severity)
        if (
            occurrence.affected_component
            and occurrence.affected_component not in existing.affected_components
        ):
            existing.affected_components.append(occurrence.affected_component)

    def records(self) -> list[AggregatedRecord]:
        """Merged records, in first-seen key order."""
        return [self._records[key] for key in self._order]


@dataclass
class ImportOptions:
    severity_filter: set[Severity] | None = None  # None = all severities
    include_plugins: list[str] = field(default_factory=list)
    exclude_plugins: list[str] = field(default_factory=list)
    min_qod: int = 0  # OpenVAS: minimum quality-of-detection threshold
    fmt: str = "xml"  # Nmap: "xml" | "grepable"
    no_snoozed: bool = False  # Nessus: skip snoozed findings


class Scanner(ABC):
    name: ClassVar[str]  # CLI subcommand slug, e.g. "burp"
    label: ClassVar[str]  # Human display name, e.g. "Burp Suite"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "__abstractmethods__", None):
            if not isinstance(cls.__dict__.get("name"), str):
                raise TypeError(
                    f"{cls.__name__} must define a class-level `name: ClassVar[str]` (CLI slug)"
                )
            if not isinstance(cls.__dict__.get("label"), str):
                raise TypeError(
                    f"{cls.__name__} must define a class-level "
                    "`label: ClassVar[str]` (display name)"
                )

    @abstractmethod
    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]: ...

    @staticmethod
    def sort_by_severity(findings: list[ScanFinding]) -> list[ScanFinding]:
        """Sort findings most-severe first (was copy-pasted across every parser)."""
        return sorted(findings, key=lambda f: SEVERITY_ORDER.index(f.severity), reverse=True)

    @staticmethod
    def max_severity(a: Severity, b: Severity) -> Severity:
        """Return the more severe of two (for merging occurrences of the same finding)."""
        return _max_severity(a, b)

    def _severity_allowed(self, severity: Severity, opts: ImportOptions) -> bool:
        if opts.severity_filter is None:
            return True
        return severity in opts.severity_filter

    def _plugin_allowed(self, plugin_id: str, opts: ImportOptions) -> bool:
        if opts.include_plugins and plugin_id not in opts.include_plugins:
            return False
        if opts.exclude_plugins and plugin_id in opts.exclude_plugins:
            return False
        return True
