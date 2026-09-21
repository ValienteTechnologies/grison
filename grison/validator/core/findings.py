"""``findings/`` document validation: library/instance/inbox finding files (FND-…,
REF-001/002/006), plus the toplevel-shape checks for ``findings/`` and
``findings/library/``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.formats import finding as finding_fmt
from grison.formats.common import FormatError
from grison.index import Index
from grison.markdown.converter import ConverterError, md_to_html
from grison.model.cvss import CvssError, parse_cvss
from grison.model.enums import Severity
from grison.validator import registry
from grison.validator.core.common import (
    _FND_KIND_TO_RULE,
    _check_txt,
    _fmt_failure,
    _read_text,
    _rel,
)
from grison.validator.core.names import _check_names
from grison.validator.refs import OfflineEvidenceResolver, cross_refs, embeds
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm


def _validate_finding_file(
    root: Path, rel: str, tier: str, evidence_dir: Path | None, cterms: list[ConfidentialTerm]
) -> list[Failure]:
    out: list[Failure] = []
    text, read_fail = _read_text(root, rel, default_rule=registry.FND_BAD_FRONTMATTER)
    if text is None:
        return [read_fail] if read_fail else []
    out.extend(_check_txt(rel, text, cterms))
    try:
        doc = finding_fmt.parse(text, path=Path(rel))
    except FormatError as e:
        out.append(_fmt_failure(rel, e, _FND_KIND_TO_RULE, registry.FND_BAD_FRONTMATTER))
        return out

    # FND-015 (severity vs. CVSS band) applies to library/instance only. An `inbox`
    # finding's severity comes straight from the scanner's own rating (see
    # grison/markdown/mapping.py's ir_to_finding), independent of any CVSS vector it
    # also carries, and routinely disagrees with the vector's naive base-score band —
    # e.g. a real nmap-parsed SSH host-key finding: severity "medium" against a
    # C:N/I:N/A:N vector, whose base score is 0.0 (informational/low band). Requiring
    # agreement pre-triage would make grison parse's own routine output invalid,
    # contradicting "grison parse writes valid findings by construction"; the agent
    # triaging an inbox finding is expected to reconcile severity/CVSS before it
    # becomes a real library/report finding, where this rule then applies in full.
    if tier != "inbox" and doc.cvss is not None:
        try:
            score = parse_cvss(doc.cvss.vector).base_score
        except CvssError:
            score = None
        if score is not None and doc.severity not in _severity_band(score):
            out.append(
                fail(
                    registry.FND_SEVERITY_CVSS_MISMATCH,
                    rel,
                    f"severity={doc.severity.value!r} but CVSS score {score} implies "
                    f"{sorted(s.value for s in _severity_band(score))}",
                )
            )

    for header, field_name in finding_fmt.SECTIONS:
        body = getattr(doc, field_name)
        out.extend(_check_finding_body(rel, header, body, tier, evidence_dir))
    return out


def _severity_band(score: float) -> set[Severity]:
    if score <= 0.0:
        return {Severity.INFORMATIONAL, Severity.LOW}
    if score < 4.0:
        return {Severity.LOW}
    if score < 7.0:
        return {Severity.MEDIUM}
    if score < 9.0:
        return {Severity.HIGH}
    return {Severity.CRITICAL}


def _check_finding_body(
    rel: str, field: str, text: str, tier: str, evidence_dir: Path | None
) -> list[Failure]:
    if not text.strip():
        return []
    out: list[Failure] = []
    if tier in ("library", "inbox"):
        found = embeds(text, prefix="evidence")
        if found:
            noun = "library" if tier == "library" else "inbox (pre-triage)"
            for ref in found:
                out.append(
                    fail(
                        registry.REF_IMAGE_IN_LIBRARY,
                        rel,
                        f"{field}: image embed {ref.path!r} — a {noun} finding has "
                        "no report to hold evidence for",
                        line=ref.line,
                    )
                )
            return out
        try:
            # headings=True: a real TipTap editor emits h1-h6 in finding fields
            # too (see grison.markdown.converter's module docstring) — must match
            # the finding adapters' own headings=True or a freshly-pulled,
            # unmodified finding whose stored HTML has a heading would fail
            # validation immediately.
            md_to_html(text, headings=True)
        except ConverterError as e:
            out.append(fail(registry.FND_BODY_NOT_CONVERTIBLE, rel, f"{field}: {e}"))
        return out

    assert evidence_dir is not None
    ref_issue = False
    for ref in embeds(text, prefix="evidence"):
        if not ref.standalone:
            out.append(
                fail(
                    registry.REF_BAD_POSITION,
                    rel,
                    f"{field}: {ref.path!r} is not alone in its block",
                    line=ref.line,
                )
            )
            ref_issue = True
        elif not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(
                fail(
                    registry.REF_UNRESOLVED,
                    rel,
                    f"{field}: {ref.path!r} does not resolve",
                    line=ref.line,
                )
            )
            ref_issue = True
    for ref in cross_refs(text, prefix="evidence"):
        if not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(
                fail(
                    registry.REF_BAD_CROSS_REFERENCE,
                    rel,
                    f"{field}: {ref.path!r} does not resolve",
                    line=ref.line,
                )
            )
            ref_issue = True
    # Only fall through to the real converter when refscan found no reference problem
    # of its own — REF-001/002/006's rule assignment never depends on the converter's
    # error text (see D3 review); this still catches everything else (raw HTML,
    # indented code blocks, other unsupported constructs — fences/blockquotes/tables
    # are supported since the 2026-09-21 grammar widening) in the common case.
    # headings=True: see the library/inbox branch above — same reason.
    if not ref_issue:
        try:
            md_to_html(text, headings=True, refs=OfflineEvidenceResolver(evidence_dir))
        except ConverterError as e:
            out.append(fail(registry.FND_BODY_NOT_CONVERTIBLE, rel, f"{field}: {e}"))
    return out


def _validate_inbox_dir(
    root: Path, index: Index | None, cterms: list[ConfidentialTerm]
) -> list[Failure]:
    """``findings/inbox/*.md`` — local-only (never synced) but still validated in
    full (tier ``inbox``): flat, ``*.md`` only, same rules as a reported finding minus
    images/evidence (there's no report yet) and minus FND-015 (see
    ``_validate_finding_file``'s comment)."""
    out: list[Failure] = []
    inbox_dir = root / "findings" / "inbox"
    if not inbox_dir.is_dir():
        return out
    for p in sorted(inbox_dir.iterdir()):
        if p.is_dir() or not p.name.endswith(".md"):
            out.append(
                fail(
                    registry.WS_UNKNOWN_FINDINGS_PATH,
                    _rel(root, p),
                    "only *.md files are allowed directly in findings/inbox/",
                )
            )
            continue
        rel = _rel(root, p)
        out.extend(_check_names(PurePosixPath(rel)))
        out.extend(_validate_finding_file(root, rel, "inbox", None, cterms))
    return out


def _check_findings_toplevel(root: Path) -> list[Failure]:
    out: list[Failure] = []
    findings = root / "findings"
    if not findings.is_dir():
        return out
    known = {"library", "reports", "inbox"}
    for p in sorted(findings.iterdir()):
        if p.name not in known:
            out.append(fail(registry.WS_UNKNOWN_FINDINGS_PATH, _rel(root, p), p.name))
    return out


def _check_library_flat(root: Path) -> list[Failure]:
    out: list[Failure] = []
    lib = root / "findings" / "library"
    if not lib.is_dir():
        return out
    for p in sorted(lib.iterdir()):
        if p.is_dir() or not p.name.endswith(".md"):
            out.append(
                fail(
                    registry.WS_UNKNOWN_FINDINGS_PATH,
                    _rel(root, p),
                    "only *.md files are allowed directly in findings/library/",
                )
            )
    return out
