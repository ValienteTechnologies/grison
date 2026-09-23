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
- ``notes`` (a parser's own free-text remarks about its extraction, e.g. Acunetix's
  "only a CVSS v4 vector was present") become a ``"finding <title>: <note>"`` warning
  each — never silently dropped, never blocking the rest of the finding.
- scanner HTML is normalized before conversion: presentational tags with a
  whitelisted GW equivalent (``<b>`` → ``<strong>``, ``<i>`` → ``<em>``) are
  rewritten via an HTML-aware pass before ``html_to_md`` runs, so real bold/italic
  prose converts instead of degrading through the lenient fallback (see
  ``_normalize_scanner_html``).

``grison parse`` output carries no machine fields at all (engine step 3 item E) — an
inbox finding is a plain :class:`~grison.formats.finding.FindingDoc`, triaged by hand
into ``findings/library/`` or a report directory; identity is established later, by
the sync engine's index, not by anything stamped into the document.
"""

from __future__ import annotations

import html
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


# Presentational tags real scanner exports emit that have a whitelisted GW
# equivalent (grammar.py's _ALLOWED_TAGS) — rewritten before html_to_md ever sees
# them, so bold/italic prose survives as real markdown instead of falling back to
# _TagStripper's plain-text degrade. Corpus-driven (2026-09): running html_to_md
# over every prose field of every burp/zap fixture found 29 of 144 fields hitting
# ConverterError, all of them `<b>`/`<i>` — no other non-whitelisted tag appears.
_TAG_ALIASES: dict[str, str] = {"b": "strong", "i": "em"}


class _TagAliasRewriter(HTMLParser):
    """Rewrites presentational HTML tags scanners emit (``<b>``, ``<i>``, ...) to
    their GW-whitelisted equivalents (``<strong>``, ``<em>``, ...) via a proper
    HTML-aware pass — never a blind string replace, which could also rewrite text
    content that happens to contain the literal substring ``<b>`` (e.g. already
    HTML-escaped prose describing markup). Any tag with no alias, and any
    attribute, passes through unchanged; a genuinely unsupported tag still fails
    in :func:`html_to_md` afterwards, same as before this rewrite ran."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # The tag name of an open <script>/<style> element, or None. HTMLParser
        # hands handle_data the RAW, un-charref-converted CDATA content of these
        # elements (that's what CDATA_CONTENT_ELEMENTS means); html.escape-ing it
        # like ordinary text would corrupt it — e.g. `a<b` inside a <script> body
        # becoming the text `a&lt;b`, which is wrong once this ever reaches a
        # fallback that emits it verbatim. Only one CDATA element is ever open at
        # a time (they don't nest), so a single slot is enough to track it.
        self._cdata_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=False)
        if tag in self.CDATA_CONTENT_ELEMENTS:
            self._cdata_tag = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=True)

    def _emit(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        out_tag = _TAG_ALIASES.get(tag, tag)
        # A boolean attribute (no value, e.g. `disabled` in `<input disabled>`)
        # passes through as a bare name — dropping it entirely would silently
        # lose it, and `name=""` would fabricate a value that was never there.
        attr_str = "".join(
            f' {name}="{html.escape(value, quote=True)}"' if value is not None else f" {name}"
            for name, value in attrs
        )
        self._out.append(f"<{out_tag}{attr_str}{'/' if self_closing else ''}>")

    def handle_endtag(self, tag: str) -> None:
        self._out.append(f"</{_TAG_ALIASES.get(tag, tag)}>")
        if tag == self._cdata_tag:
            self._cdata_tag = None

    def handle_data(self, data: str) -> None:
        if self._cdata_tag is not None:
            self._out.append(data)
        else:
            self._out.append(html.escape(data))

    def result(self) -> str:
        return "".join(self._out)


def _normalize_scanner_html(raw: str) -> str:
    """Apply :class:`_TagAliasRewriter` to ``raw``. A no-op for anything without a
    ``<`` (avoids paying for a parse pass on plain text, the common case for a
    short field)."""
    if "<" not in raw:
        return raw
    rewriter = _TagAliasRewriter()
    rewriter.feed(raw)
    rewriter.close()
    return rewriter.result()


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


def _prose_to_md(markup: str, field: str, warnings: list[str]) -> str:
    if not markup.strip():
        return ""
    markup = _normalize_scanner_html(markup)
    try:
        return html_to_md(markup).strip()
    except ConverterError as e:
        stripper = _TagStripper()
        stripper.feed(markup)
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
    title = ir.title.strip() or "Untitled finding"
    for note in ir.notes:
        warnings.append(f"finding {title}: {note}")

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
        "title": title,
        "description": _prose_to_md(ir.description, "description", warnings),
        "impact": _prose_to_md(ir.impact, "impact", warnings),
        "mitigation": _prose_to_md(ir.mitigation, "mitigation", warnings),
        "replication_steps": _prose_to_md(ir.replication_steps, "replication_steps", warnings),
        "references": _prose_to_md(ir.references, "references", warnings),
    }
    finding = FindingDoc.model_validate(data, context={"tier": tier})
    return MappingResult(finding=finding, warnings=warnings)
