"""Map the vendored scanner IR (:mod:`grison.scanners.ir`) to a format-v2 inbox
finding (:class:`grison.formats.finding.FindingDoc`, tier ``inbox``).

Settled rules (shape.md §Phase 4):
- scanner severity (INFO…CRITICAL) → :class:`~grison.model.Severity` 1:1.
- ``affected_components`` → ``affected_entities`` (one per line) — *not* prepended
  to replication_steps the way gw-import's importer did (GW has a real field).
- ``cwe`` → normalized ``CWE-N``; unknown-to-index values **warn and drop**.
- an unparseable CVSS vector is **dropped with a warning** (parse stays faithful —
  never lose a whole finding over one bad field).
- ``finding_type`` isn't emitted by scanners → supplied by the caller (a per-scanner
  default, overridable by the CLI).

``grison parse`` output carries no machine fields at all (engine step 3 item E) — an
inbox finding is a plain :class:`~grison.formats.finding.FindingDoc`, triaged by hand
into ``findings/library/`` or a report directory; identity is established later, by
the sync engine's index, not by anything stamped into the document.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from grison.markdown.converter import ConverterError, escape_literal_text_to_md, html_to_md
from grison.model.cvss import CvssError, parse_cvss
from grison.model.cwe import is_known_cwe, normalize_cwe
from grison.model.enums import FindingType, Severity

if TYPE_CHECKING:
    from grison.formats.finding import FindingDoc
from grison.scanners.ir import ScanFinding

# Scanners don't emit a finding type; pick a sensible default by tool.
_DEFAULT_FINDING_TYPE: dict[str, FindingType] = {
    "nessus": FindingType.NETWORK,
    "nmap": FindingType.NETWORK,
    "openvas": FindingType.NETWORK,
    "qualys": FindingType.NETWORK,
    "sslyze": FindingType.NETWORK,
    "zap": FindingType.WEB,
    "burp": FindingType.WEB,
    "acunetix": FindingType.WEB,
}


def default_finding_type(scanner_name: str) -> FindingType:
    return _DEFAULT_FINDING_TYPE.get(scanner_name, FindingType.NETWORK)


@dataclass
class MappingResult:
    finding: FindingDoc
    warnings: list[str]


class _TagStripper(HTMLParser):
    """Lenient fallback: reduce HTML to text when it's outside the GW whitelist.
    Table cells are joined with `` | `` — without a separator, row values would run
    together into one ambiguous token (``22/tcpsshOpenSSH``).

    ``handle_data`` already receives HTML-unescaped text (``HTMLParser``'s default
    ``convert_charrefs=True``) — :meth:`text` must NOT run ``html.unescape`` on the
    result again: that would silently double-decode a literal, safe entity NAME
    sitting in the source text (e.g. Burp's own remediation prose spells out
    ``&amp;lt;`` to *display* the four characters ``&lt;`` — already correctly
    decoded once to ``&lt;`` here — into the real ``<`` character, corrupting the
    text). :func:`escape_literal_text_to_md` in :meth:`text`'s caller is what makes
    the result safe to embed in a markdown field, not a second unescape pass."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._pending_cell_sep = False

    def handle_data(self, data: str) -> None:
        if data.strip() and self._pending_cell_sep:
            self._parts.append(" | ")
            self._pending_cell_sep = False
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("li", "p", "br", "ul", "ol", "tr", "div"):
            self._parts.append("\n")
            self._pending_cell_sep = False

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._pending_cell_sep = True
        elif tag in ("tr", "table", "thead", "tbody"):
            self._pending_cell_sep = False

    def text(self) -> str:
        lines = [ln.strip() for ln in "".join(self._parts).splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()


def _prose_to_md(html: str, field: str, warnings: list[str]) -> str:
    if not html.strip():
        return ""
    try:
        return html_to_md(html).strip()
    except ConverterError as e:
        stripper = _TagStripper()
        stripper.feed(html)
        warnings.append(f"{field}: HTML outside GW whitelist, degraded to text ({e})")
        return escape_literal_text_to_md(stripper.text())


def ir_to_finding(
    ir: ScanFinding,
    *,
    finding_type: FindingType,
    tier: str = "inbox",
) -> MappingResult:
    """Convert one scanner IR finding into a validated format-v2 finding + warnings.

    ``tier`` defaults to ``"inbox"`` — every real ``grison parse`` call writes to
    ``findings/inbox/`` (BRIEF workspace layout); it's a parameter only so a caller
    validating a standalone finding against a different tier's rules (tests) can ask
    for one directly rather than round-tripping through a file path."""
    # Imported here, not at module level: grison.formats.finding pulls in
    # grison.markdown.frontmatter, which (via this package's __init__) would import
    # this very module back — a real cycle only at *module load* time, not at call
    # time, once everything has finished initializing.
    from grison.formats.finding import FindingCvss, FindingDoc

    warnings: list[str] = []

    cvss = None
    if ir.cvss_vector.strip():
        vector = ir.cvss_vector.strip()
        try:
            parse_cvss(vector)
            cvss = FindingCvss(vector=vector)
        except CvssError:
            warnings.append(f"dropped invalid CVSS vector {ir.cvss_vector!r}")

    cwe_list: list[str] = []
    if ir.cwe.strip():
        norm = normalize_cwe(ir.cwe)
        if norm and is_known_cwe(norm):
            cwe_list = [norm]
        else:
            warnings.append(f"dropped unknown CWE {ir.cwe!r}")

    affected = "\n".join(ir.affected_components) if ir.affected_components else None

    data = {
        "severity": Severity(ir.severity.value),
        "finding_type": finding_type,
        "cvss": cvss,
        "cwe": cwe_list,
        "tags": list(ir.tags),
        "affected_entities": affected,
        "title": ir.title.strip() or "Untitled finding",
        "description": _prose_to_md(ir.description, "description", warnings),
        "impact": _prose_to_md(ir.impact, "impact", warnings),
        "mitigation": _prose_to_md(ir.mitigation, "mitigation", warnings),
        "replication_steps": _prose_to_md(ir.replication_steps, "replication_steps", warnings),
        "references": _prose_to_md(ir.references, "references", warnings),
    }
    finding = FindingDoc.model_validate(data, context={"tier": tier})
    return MappingResult(finding=finding, warnings=warnings)
