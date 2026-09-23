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


def _serialize_attrs(attrs: list[tuple[str, str | None]]) -> str:
    """Render an ``HTMLParser`` attribute list back to source form, shared by
    every rewriter pass below. A boolean attribute (no value, e.g. ``disabled``
    in ``<input disabled>``) passes through as a bare name — dropping it
    entirely would silently lose it, and ``name=""`` would fabricate a value
    that was never there."""
    return "".join(
        f' {name}="{html.escape(value, quote=True)}"' if value is not None else f" {name}"
        for name, value in attrs
    )


# Block-level start tags whose arrival auto-closes a currently-open <p> (HTML5's
# own implied-end-tag rule for <p>, applied narrowly — see _StructureNormalizer).
_P_CLOSE_TRIGGERS = {
    "p",
    "ul",
    "ol",
    "li",
    "div",
    "table",
    "pre",
    "blockquote",
    "dl",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


class _StructureNormalizer(HTMLParser):
    """HTML5-lite structural cleanup, run before :class:`_TagAliasRewriter`, over
    the handful of fixed ways real scanner HTML violates well-formedness (Qualys
    prose above all — entire multi-paragraph fields built from bare ``<P>`` with
    no ``</P>`` anywhere):

    - An open ``<p>`` is auto-closed when another block-level start tag in
      ``_P_CLOSE_TRIGGERS`` arrives, or at end-of-input — but ONLY when ``<p>``
      is still the INNERMOST open element at that point (this pass tracks a
      real stack of every open tag to check that, not just a bare "is some p
      open somewhere" flag). If something else opened inside the ``<p>`` is
      still open too (e.g. a ``<b>`` a real closing tag never arrived for), this
      pass does nothing — it never guesses which of two unclosed elements to
      close, and leaves the whole thing exactly as malformed as it arrived, for
      :func:`html_to_md`'s own fallback to handle, same as before this pass
      existed. This is HTML5's own implied-end-tag rule for ``<p>``, applied
      narrowly: nothing else is ever auto-closed, and a mismatched *closing*
      tag is never repaired.
    - ``<div>`` is unwrapped: its own start/end tags are dropped, its children
      pass through untouched. Scanner-prose ``<div>`` wrapping is always
      presentational (e.g. Acunetix's ``<div class="bb-coolbox">``) — this pass
      never sees a real GW evidence ``<div>``, a construct scanner exports don't
      have.
    - ``<dl>``/``<dt>``/``<dd>`` becomes ``<ul><li><strong>dt</strong>
      dd</li></ul>`` (GW's allowed vocabulary has no definition-list construct).
      A ``<dt>`` with no following ``<dd>`` still closes its own ``<li>`` (on the
      next ``<dt>``, or ``</dl>``/end-of-input) rather than leaking an unclosed
      item.

    Any other tag — including one the converter doesn't support at all, like
    ``<limit>`` — passes through completely unchanged, so it still fails in
    :func:`html_to_md` exactly as before this pass existed; this pass never
    invents leniency for genuinely unsupported markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # Every tag currently open, in nesting order — including "div"/"dl",
        # even though their own start/end tags are never written to _out, so a
        # <p> auto-close only fires when <p> is genuinely the innermost open
        # element (see _open) rather than one this pass has lost track of.
        self._stack: list[str] = []
        # One entry per currently-open <dl>, true while that level's current
        # <li> (opened by a <dt>) hasn't been closed by a following <dt> or
        # </dl> yet — see _open/_close's "dt"/"dl" handling.
        self._dl_li_open: list[bool] = []
        # The tag name of an open <script>/<style> element, or None — same
        # raw-CDATA tracking as _TagAliasRewriter (see its own docstring for
        # why handle_data must not html.escape this content).
        self._cdata_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)
        if tag in self.CDATA_CONTENT_ELEMENTS:
            self._cdata_tag = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing tag (e.g. <br/>) has no separate closing event to
        # balance — same one-call treatment _TreeBuilder itself gives it.
        self._open(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self._close(tag)
        if tag == self._cdata_tag:
            self._cdata_tag = None

    def handle_data(self, data: str) -> None:
        if self._cdata_tag is not None:
            self._out.append(data)
        else:
            self._out.append(html.escape(data))

    def result(self) -> str:
        # End-of-input is itself a <p>-closing trigger (class docstring) — a
        # real Qualys field routinely ends on a final bare <P> with nothing
        # after it. An unclosed <dl>/<dt> at end-of-input is flushed the same
        # way, for the same reason.
        if self._stack and self._stack[-1] == "p":
            self._out.append("</p>")
            self._stack.pop()
        while self._dl_li_open:
            if self._dl_li_open.pop():
                self._out.append("</li>")
            self._out.append("</ul>")
        return "".join(self._out)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dt" and self._dl_li_open:
            if self._dl_li_open[-1]:
                self._out.append("</li>")
            self._out.append("<li><strong>")
            self._dl_li_open[-1] = True
            return
        if tag == "dd" and self._dl_li_open:
            return  # dd content flows directly into the <li> its <dt> opened
        if tag in _P_CLOSE_TRIGGERS and self._stack and self._stack[-1] == "p":
            self._out.append("</p>")
            self._stack.pop()
        if tag == "div":
            self._stack.append(tag)
            return
        if tag == "dl":
            self._dl_li_open.append(False)
            self._stack.append(tag)
            self._out.append("<ul>")
            return
        self._out.append(f"<{tag}{_serialize_attrs(attrs)}>")
        if tag != "br":
            self._stack.append(tag)

    def _close(self, tag: str) -> None:
        if tag == "dt" and self._dl_li_open:
            self._out.append("</strong> ")
            return
        if tag == "dd" and self._dl_li_open:
            return
        if tag == "dl":
            if self._stack and self._stack[-1] == "dl":
                self._stack.pop()
            if self._dl_li_open:
                if self._dl_li_open.pop():
                    self._out.append("</li>")
                self._out.append("</ul>")
            return
        if tag == "div":
            if self._stack and self._stack[-1] == "div":
                self._stack.pop()
            return
        # A mismatched closing tag (stack top isn't `tag`) is left exactly as
        # malformed as it arrived — this pass never repairs one, only tracks
        # what it can to keep its OWN <p> auto-close accurate (class
        # docstring). A genuine match pops the stack so an ancestor's own
        # later close, or a subsequent <p>-close check, sees the right top.
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        self._out.append(f"</{tag}>")


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
        self._out.append(f"<{out_tag}{_serialize_attrs(attrs)}{'/' if self_closing else ''}>")

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
    """Apply :class:`_StructureNormalizer` then :class:`_TagAliasRewriter` to
    ``raw``. A no-op for anything without a ``<`` (avoids paying for two parse
    passes on plain text, the common case for a short field)."""
    if "<" not in raw:
        return raw
    structure = _StructureNormalizer()
    structure.feed(raw)
    structure.close()
    rewriter = _TagAliasRewriter()
    rewriter.feed(structure.result())
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
