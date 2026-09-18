"""``validate_workspace`` — the ONE offline validator for workspace format v2.

Never raises on a malformed DOCUMENT: every content problem, however the input is
broken, becomes a :class:`~grison.validator.registry.Failure` in the returned list.
It DOES raise — :class:`~grison.validator.errors.ValidationScopeError` — for a
``paths=`` argument that doesn't resolve to a real, in-workspace, validated location;
see the ``_Scope`` section below for why silently matching nothing is a worse failure
mode than raising. Nothing here contacts a network or needs credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from markdown_it.tree import SyntaxTreeNode

from grison import manifest as manifest_mod
from grison.formats import finding as finding_fmt
from grison.formats import mirrors as mirrors_fmt
from grison.formats import narrative as narrative_fmt
from grison.formats import note as note_fmt
from grison.formats import wiki as wiki_fmt
from grison.formats.common import NAME_RE, FormatError
from grison.hashing import digest_text
from grison.index import Index, IndexFileError, IndexKind
from grison.markdown.converter import ConverterError, md_to_html
from grison.model.cvss import CvssError, parse_cvss
from grison.model.enums import Severity
from grison.remote.creds import load as load_creds
from grison.validator import registry, wikibody
from grison.validator import terms as terms_mod
from grison.validator.errors import ValidationScopeError
from grison.validator.mirrors import expected_digest
from grison.validator.refs import OfflineEvidenceResolver, cross_refs, embeds, stems, wiki_images
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm

# (kind FormatError -> rule id) per document family. A kind not present here falls
# back to the family's own catch-all rule.
_FND_KIND_TO_RULE: dict[str, str] = {
    "unknown_field": registry.FND_UNKNOWN_FIELD,
    "missing_field": registry.FND_MISSING_FIELD,
    "bad_severity": registry.FND_BAD_SEVERITY,
    "bad_finding_type": registry.FND_BAD_FINDING_TYPE,
    "bad_cvss": registry.FND_BAD_CVSS,
    "bad_cwe": registry.FND_BAD_CWE,
    "bad_tags": registry.FND_BAD_TAGS,
    "affected_entities_on_library": registry.FND_AFFECTED_ENTITIES_ON_LIBRARY,
    "missing_section": registry.FND_MISSING_SECTION,
    "unknown_section": registry.FND_UNKNOWN_SECTION,
    "duplicate_section": registry.FND_DUPLICATE_SECTION,
    "sections_out_of_order": registry.FND_SECTIONS_OUT_OF_ORDER,
    "missing_title": registry.FND_BAD_TITLE,
    "empty_title": registry.FND_BAD_TITLE,
    "unexpected_content": registry.FND_UNEXPECTED_CONTENT,
}
_WIKI_KIND_TO_RULE: dict[str, str] = {
    "unknown_field": registry.WIKI_UNKNOWN_FIELD,
    "missing_field": registry.WIKI_BAD_TITLE,
    "bad_title": registry.WIKI_BAD_TITLE,
    "bad_priority": registry.WIKI_BAD_PRIORITY,
    "bad_tags": registry.WIKI_BAD_TAGS,
}


def _fmt_failure(rel: str, e: FormatError, kind_map: dict[str, str], default_rule: str) -> Failure:
    rule_id = kind_map.get(e.kind, default_rule)
    return fail(rule_id, rel, e.detail or e.kind, line=e.line)


def _read_text(root: Path, rel: str, *, default_rule: str) -> tuple[str | None, Failure | None]:
    try:
        # newline="" disables Python's universal-newline translation — a real CRLF
        # byte sequence on disk must reach WIKI-009's own check as "\r\n", not get
        # silently rewritten to "\n" by the read itself.
        with (root / rel).open(encoding="utf-8", newline="") as fh:
            return fh.read(), None
    except (OSError, UnicodeDecodeError) as e:
        return None, fail(default_rule, rel, f"cannot read file: {e}")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --- path classification -----------------------------------------------------------


def _is_valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def _check_names(rel: PurePosixPath) -> list[Failure]:
    """WS-001 against every path segment of ``rel`` (dotfiles like ``.report.yml``
    are exempt — grison itself names its own mirrors with a leading dot)."""
    out: list[Failure] = []
    for part in rel.parts:
        if part.startswith("."):
            continue
        if not _is_valid_name(part):
            out.append(fail(registry.WS_BAD_NAME, rel.as_posix(), f"segment {part!r}"))
    return out


# --- findings/ -----------------------------------------------------------------------


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
                    registry.FND_SEVERITY_CVSS_MISMATCH, rel,
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
                out.append(fail(registry.REF_IMAGE_IN_LIBRARY, rel,
                                f"{field}: image embed {ref.path!r} — a {noun} finding has "
                                "no report to hold evidence for", line=ref.line))
            return out
        try:
            md_to_html(text)
        except ConverterError as e:
            out.append(fail(registry.FND_BODY_NOT_CONVERTIBLE, rel, f"{field}: {e}"))
        return out

    assert evidence_dir is not None
    ref_issue = False
    for ref in embeds(text, prefix="evidence"):
        if not ref.standalone:
            out.append(fail(registry.REF_BAD_POSITION, rel,
                            f"{field}: {ref.path!r} is not alone in its block", line=ref.line))
            ref_issue = True
        elif not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(fail(registry.REF_UNRESOLVED, rel,
                            f"{field}: {ref.path!r} does not resolve", line=ref.line))
            ref_issue = True
    for ref in cross_refs(text, prefix="evidence"):
        if not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(fail(registry.REF_BAD_CROSS_REFERENCE, rel,
                            f"{field}: {ref.path!r} does not resolve", line=ref.line))
            ref_issue = True
    # Only fall through to the real converter when refscan found no reference problem
    # of its own — REF-001/002/006's rule assignment never depends on the converter's
    # error text (see D3 review); this still catches everything else (tables, ATX
    # headings, raw HTML, unsupported constructs) in the common case.
    if not ref_issue:
        try:
            md_to_html(text, refs=OfflineEvidenceResolver(evidence_dir))
        except ConverterError as e:
            out.append(fail(registry.FND_BODY_NOT_CONVERTIBLE, rel, f"{field}: {e}"))
    return out


def _check_txt(rel: str, text: str, cterms: list[ConfidentialTerm]) -> list[Failure]:
    out: list[Failure] = []
    for lineno, phrase in terms_mod.find_banned_phrases(text):
        out.append(fail(registry.TXT_BANNED_PHRASE, rel, f"banned phrase: {phrase!r}",
                        line=lineno))
    for term in cterms:
        if terms_mod.path_allows(rel, term.allowed_prefix):
            continue
        for lineno in terms_mod.find_term(text, term.term):
            out.append(
                fail(registry.TXT_CONFIDENTIAL_TERM, rel,
                    f"confidential term: {terms_mod.mask(term.term)}", line=lineno)
            )
    return out


@dataclass
class _EmbedHit:
    path: str  # relative to workspace root
    ref: str  # the "evidence/x.png" or "images/x.png" spelling
    caption: str
    doc_rel: str
    line: int


def _check_mirror(root: Path, rel: str, text: str) -> list[Failure]:
    digest = expected_digest(root, rel)
    if digest is None:
        return []
    if digest_text(text) != digest:
        return [fail(registry.WS_MIRROR_EDITED, rel, "content differs from the last sync")]
    return []


def _validate_report_dir(
    root: Path, report_dir: PurePosixPath, index: Index | None, cterms: list[ConfidentialTerm]
) -> list[Failure]:
    out: list[Failure] = []
    rel_dir = report_dir.as_posix()
    # WS-004 (retired): a report directory's name is a stable handle, not derived
    # data — a freshly-pulled one is slug(title) with no numeric prefix at all, and an
    # existing v1 "<id>-<slug>" name stays valid forever (D4). Only WS-001 (name
    # charset, via _check_names above) and IDX-003 (below — a document inside a
    # directory not indexed as gw.report) apply to a report directory's name/identity.
    out.extend(_check_names(report_dir))

    indexed = index is not None and _entry_kind(index, rel_dir) == IndexKind.GW_REPORT
    abs_dir = root / report_dir
    evidence_dir = abs_dir / "evidence"

    known_top = {"evidence", "narrative", "notes", ".report.yml", "project.md"}
    for p in sorted(abs_dir.iterdir()) if abs_dir.is_dir() else []:
        if p.name in known_top or (p.is_file() and p.name.endswith(".md")):
            continue
        out.append(fail(registry.WS_UNKNOWN_FINDINGS_PATH, _rel(root, p),
                        "unexpected entry directly inside a report directory"))
    for sub in ("narrative", "notes"):
        subdir = abs_dir / sub
        if subdir.is_dir():
            for p in sorted(subdir.iterdir()):
                if p.is_dir() or not p.name.endswith(".md"):
                    out.append(fail(registry.WS_UNKNOWN_FINDINGS_PATH, _rel(root, p),
                                    f"only *.md files are allowed directly in {sub}/"))

    all_files = sorted(p for p in abs_dir.rglob("*") if p.is_file())
    if index is not None and not indexed:
        for p in all_files:
            out.append(fail(registry.IDX_UNINDEXED_REPORT_DIR, _rel(root, p), rel_dir))

    embed_hits: list[_EmbedHit] = []

    for md_path in sorted(abs_dir.glob("*.md")):
        if md_path.name == "project.md":  # a mirror (§3.3), not a finding — handled below
            continue
        rel = _rel(root, md_path)
        out.extend(_check_names(PurePosixPath(rel)))
        out.extend(_validate_finding_file(root, rel, "instance", evidence_dir, cterms))
        text = md_path.read_text(encoding="utf-8", errors="replace")
        for ref in embeds(text, prefix="evidence"):
            embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    narrative_order = _read_narrative_order(abs_dir / ".report.yml")
    narrative_dir = abs_dir / "narrative"
    if narrative_dir.is_dir():
        for md_path in sorted(narrative_dir.glob("*.md")):
            rel = _rel(root, md_path)
            out.extend(_check_names(PurePosixPath(rel)))
            if narrative_order is not None and md_path.stem not in narrative_order:
                out.append(fail(registry.REP_UNKNOWN_NARRATIVE_FIELD, rel,
                                f"field {md_path.stem!r} is not in {sorted(narrative_order)}"))
            ntext, read_fail = _read_text(root, rel, default_rule=registry.REP_BODY_NOT_CONVERTIBLE)
            if ntext is None:
                out.extend([read_fail] if read_fail else [])
                continue
            out.extend(_check_txt(rel, ntext, cterms))
            doc = narrative_fmt.parse(ntext, path=md_path)
            out.extend(_check_narrative_body(rel, doc.body, evidence_dir))
            for ref in embeds(doc.body, prefix="evidence"):
                embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    notes_dir = abs_dir / "notes"
    if notes_dir.is_dir():
        for md_path in sorted(notes_dir.glob("*.md")):
            rel = _rel(root, md_path)
            out.extend(_check_names(PurePosixPath(rel)))
            out.extend(_validate_note_file(root, rel, index, cterms, evidence_dir))

    if evidence_dir.is_dir():
        filenames = sorted(p.name for p in evidence_dir.iterdir() if p.is_file())
        for stem, group in stems(filenames).items():
            if len(group) > 1:
                for name in group:
                    out.append(
                        fail(registry.REF_STEM_COLLISION,
                            _rel(root, evidence_dir / name), f"shares stem {stem!r} with "
                            f"{[g for g in group if g != name]}")
                    )
        for p in sorted(evidence_dir.iterdir()):
            if p.is_file():
                out.extend(_check_names(PurePosixPath(_rel(root, p))))
                if index is not None:
                    out.extend(_check_index_kind(root, p, index, IndexKind.GW_EVIDENCE))

    # REF-004: disagreeing non-empty captions for the same referenced file
    by_path: dict[str, list[_EmbedHit]] = {}
    for hit in embed_hits:
        by_path.setdefault(hit.path, []).append(hit)
    for path, hits in by_path.items():
        captions = {h.caption for h in hits if h.caption.strip()}
        if len(captions) > 1:
            for h in hits:
                out.append(
                    fail(registry.REF_CAPTION_CONFLICT, h.doc_rel,
                        f"{path!r} captioned {sorted(captions)} across this report", line=h.line)
                )

    for name in (".report.yml", "project.md"):
        p = abs_dir / name
        if p.is_file():
            rel = _rel(root, p)
            text = p.read_text(encoding="utf-8", errors="replace")
            out.extend(_check_mirror(root, rel, text))
            try:
                if name == ".report.yml":
                    mirrors_fmt.parse_report_meta(text, path=p)
                else:
                    mirrors_fmt.parse_project_context(text, path=p)
            except FormatError as e:
                out.append(fail(registry.WS_MIRROR_MALFORMED, rel, e.detail or e.kind))

    return out


def _read_narrative_order(meta_path: Path) -> set[str] | None:
    """``.report.yml``'s recorded ``narrative_order`` (REP-003's only offline source
    of truth for "which fields are real" — the validator never contacts Ghostwriter).
    ``None`` when there is nothing to compare against yet: no ``.report.yml`` (never
    synced), a malformed one (``WS-010`` already reports that separately), or one
    with an empty ``narrative_order`` — matches the "nothing recorded yet" convention
    ``WS-009``'s digest check uses."""
    if not meta_path.is_file():
        return None
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
        doc = mirrors_fmt.parse_report_meta(text, path=meta_path)
    except FormatError:
        return None
    return set(doc.narrative_order) or None


def _check_narrative_body(rel: str, text: str, evidence_dir: Path) -> list[Failure]:
    if not text.strip():
        return []
    out: list[Failure] = []
    ref_issue = False
    for ref in embeds(text, prefix="evidence"):
        if not ref.standalone:
            out.append(fail(registry.REF_BAD_POSITION, rel,
                            f"{ref.path!r} is not alone in its block", line=ref.line))
            ref_issue = True
        elif not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(fail(registry.REF_UNRESOLVED, rel, f"{ref.path!r} does not resolve",
                            line=ref.line))
            ref_issue = True
    for ref in cross_refs(text, prefix="evidence"):
        if not (evidence_dir / ref.path[len("evidence/") :]).is_file():
            out.append(fail(registry.REF_BAD_CROSS_REFERENCE, rel, f"{ref.path!r} does not "
                            "resolve", line=ref.line))
            ref_issue = True
    if not ref_issue:
        try:
            md_to_html(text, headings=True, refs=OfflineEvidenceResolver(evidence_dir))
        except ConverterError as e:
            out.append(fail(registry.REP_BODY_NOT_CONVERTIBLE, rel, str(e)))
    return out


def _validate_note_file(
    root: Path, rel: str, index: Index | None, cterms: list[ConfidentialTerm],
    evidence_dir: Path,
) -> list[Failure]:
    out: list[Failure] = []
    text, read_fail = _read_text(root, rel, default_rule=registry.REP_BAD_NOTE)
    if text is None:
        return [read_fail] if read_fail else []
    out.extend(_check_txt(rel, text, cterms))
    try:
        doc = note_fmt.parse(text, path=Path(rel))
    except FormatError as e:
        out.append(fail(registry.REP_BAD_NOTE, rel, e.detail or e.kind, line=e.line))
        return out

    is_indexed = index is not None and _entry_kind(index, rel) == IndexKind.GW_PROJECT_NOTE
    if index is not None:
        if is_indexed and not doc.has_frontmatter:
            out.append(fail(registry.REP_BAD_NOTE, rel,
                            "indexed note has no frontmatter (expected a mirrored note)"))
        elif not is_indexed and doc.has_frontmatter:
            out.append(fail(registry.REP_BAD_NOTE, rel,
                            "unindexed note has frontmatter (a new local note must not)"))

    out.extend(_check_narrative_body(rel, doc.body, evidence_dir))
    return out


# --- methodology/ ----------------------------------------------------------------


@dataclass(frozen=True)
class WikiSlugs:
    """Every book/chapter/page slug this workspace's own directory names imply, for
    WIKI-007's internal-link resolution. Computed once per :func:`validate_workspace`
    call from the tree itself (there is no network to ask BookStack)."""

    books: frozenset[str]
    chapters: dict[str, frozenset[str]]
    pages: dict[str, frozenset[str]]


def _collect_one_book_slugs(book_dir: Path) -> tuple[frozenset[str], frozenset[str]]:
    """``(chapter slugs, page slugs)`` for one book-shaped directory (a book under
    ``methodology/library``, or one engagement under ``methodology/checklists`` —
    same shape, ``[<chapter>/]*.md`` plus an optional ``images/``)."""
    cdirs = [c for c in book_dir.iterdir() if c.is_dir() and c.name != "images"]
    chapters = frozenset(c.name for c in cdirs)
    page_stems = {p.stem for p in book_dir.glob("*.md")}
    for c in cdirs:
        page_stems |= {p.stem for p in c.glob("*.md")}
    return chapters, frozenset(page_stems)


def _collect_wiki_slugs(base: Path) -> WikiSlugs:
    """Every book (direct subdirectory of ``base``, ``.shelves`` excluded) and its
    chapter/page slugs. ``base`` is ``methodology/library`` for the real wiki."""
    books: set[str] = set()
    chapters: dict[str, frozenset[str]] = {}
    pages: dict[str, frozenset[str]] = {}
    if not base.is_dir():
        return WikiSlugs(frozenset(), {}, {})
    for bdir in sorted(p for p in base.iterdir() if p.is_dir() and p.name != ".shelves"):
        books.add(bdir.name)
        chapters[bdir.name], pages[bdir.name] = _collect_one_book_slugs(bdir)
    return WikiSlugs(frozenset(books), chapters, pages)


def _merge_wiki_slugs(a: WikiSlugs, b: WikiSlugs) -> WikiSlugs:
    chapters = dict(a.chapters)
    for k, v in b.chapters.items():
        chapters[k] = chapters.get(k, frozenset()) | v
    pages = dict(a.pages)
    for k, v in b.pages.items():
        pages[k] = pages.get(k, frozenset()) | v
    return WikiSlugs(a.books | b.books, chapters, pages)


def _checklist_slugs(engagement_dir: Path, library_slugs: WikiSlugs) -> WikiSlugs:
    """A checklist engagement's own internal-link/image namespace: its own tree
    (any NEW page/chapter an agent adds while filling it in) PLUS the full
    ``methodology/library`` namespace (format choice — a checklist is a working copy
    of library content, so an inherited internal link still legitimately names the
    ORIGINAL library book it was copied from; resolving against the copy alone would
    make every such inherited link fail)."""
    chapters, pages = _collect_one_book_slugs(engagement_dir)
    own = WikiSlugs(
        frozenset({engagement_dir.name}), {engagement_dir.name: chapters},
        {engagement_dir.name: pages},
    )
    return _merge_wiki_slugs(library_slugs, own)


def _bs_host(root: Path) -> str | None:
    """The workspace's own BookStack host, if ``.grison/env``/env vars name one —
    used only to recognize an ABSOLUTE internal link to it (WIKI-007); never
    contacted."""
    try:
        url = load_creds(root).bs_url
    except Exception:  # noqa: BLE001 — malformed/partial creds must never break validate
        return None
    return urlsplit(url).netloc or None


_INTERNAL_LINK_RE = re.compile(r"^/books/(?P<book>[^/]+)/(?P<kind>page|chapter)/(?P<slug>[^/]+)/?$")


def _check_internal_links(
    rel: str, tree: SyntaxTreeNode, slugs: WikiSlugs, bs_host: str | None
) -> list[Failure]:
    out: list[Failure] = []
    for node in tree.walk():
        if node.type != "link":
            continue
        href = node.attrs.get("href")
        if not isinstance(href, str):
            continue
        path_part: str | None
        if href.startswith(("http://", "https://")):
            parsed = urlsplit(href)
            path_part = parsed.path if bs_host and parsed.netloc == bs_host else None
        elif href.startswith("/books/"):
            path_part = href
        else:
            path_part = None
        if path_part is None:
            continue
        m = _INTERNAL_LINK_RE.match(path_part)
        if not m:
            continue
        book, kind, slug = m.group("book"), m.group("kind"), m.group("slug")
        target = slugs.chapters.get(book, frozenset()) if kind == "chapter" \
            else slugs.pages.get(book, frozenset())
        if book not in slugs.books or slug not in target:
            out.append(fail(registry.WIKI_BROKEN_INTERNAL_LINK, rel, f"{href!r} does not resolve"))
    return out


def _validate_book_dir(
    root: Path, book_dir: PurePosixPath, index: Index | None, cterms: list[ConfidentialTerm],
    slugs: WikiSlugs, bs_host: str | None, *, check_mirror_digest: bool = True,
    noun: str = "book",
) -> list[Failure]:
    """Validates one book-shaped directory: a real ``methodology/library/<book>``, or
    (``check_mirror_digest=False``, ``noun="checklist"``) one
    ``methodology/checklists/<engagement>`` working copy — same shape, same WIKI-…/
    REF-… rules; a checklist's ``.book.yml``/``.chapter.yml`` are copies, so WS-009
    (mirror hand-edited) does not apply to them (WS-010 schema-checking still does)."""
    out: list[Failure] = []
    out.extend(_check_names(book_dir))
    abs_book = root / book_dir
    images_dir = abs_book / "images"

    page_files: list[Path] = sorted(abs_book.glob("*.md"))
    chapter_dirs = sorted(p for p in abs_book.iterdir() if p.is_dir() and p.name != "images")
    for p in sorted(abs_book.iterdir()):
        if p.name == "images" or p in chapter_dirs or (p.is_file() and p.name.endswith(".md")):
            continue
        if p.name == ".book.yml":
            continue
        out.append(fail(registry.WS_UNKNOWN_METHODOLOGY_PATH, _rel(root, p),
                        f"unexpected entry directly inside a {noun} directory"))
    for cdir in chapter_dirs:
        out.extend(_check_names(PurePosixPath(_rel(root, cdir))))
        page_files.extend(sorted(cdir.glob("*.md")))
        for p in sorted(cdir.iterdir()):
            if p.name == ".chapter.yml" or (p.is_file() and p.name.endswith(".md")):
                continue
            out.append(fail(registry.WS_UNKNOWN_METHODOLOGY_PATH, _rel(root, p),
                            "unexpected entry directly inside a chapter directory"))

    embed_hits: list[_EmbedHit] = []
    for page in page_files:
        rel = _rel(root, page)
        out.extend(_check_names(PurePosixPath(rel)))
        chapter = page.parent != abs_book
        out.extend(_validate_wiki_page(root, rel, page, is_in_chapter=chapter, cterms=cterms,
                                       slugs=slugs, bs_host=bs_host))
        text = page.read_text(encoding="utf-8", errors="replace")
        try:
            doc = wiki_fmt.parse(text, path=page)
        except FormatError:
            continue
        for ref in wiki_images(doc.body):
            embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    if images_dir.is_dir():
        filenames = sorted(p.name for p in images_dir.iterdir() if p.is_file())
        for stem, group in stems(filenames).items():
            if len(group) > 1:
                for name in group:
                    out.append(
                        fail(registry.REF_STEM_COLLISION, _rel(root, images_dir / name),
                            f"shares stem {stem!r} with {[g for g in group if g != name]}")
                    )
        for p in sorted(images_dir.iterdir()):
            if p.is_file():
                out.extend(_check_names(PurePosixPath(_rel(root, p))))
                if index is not None:
                    out.extend(_check_index_kind(root, p, index, IndexKind.BS_IMAGE))

    by_ref: dict[str, list[_EmbedHit]] = {}
    for hit in embed_hits:
        by_ref.setdefault(hit.ref.rsplit("/", 1)[-1], []).append(hit)
    for _fname, hits in by_ref.items():
        captions = {h.caption for h in hits if h.caption.strip()}
        if len(captions) > 1:
            for h in hits:
                out.append(
                    fail(registry.REF_CAPTION_CONFLICT, h.doc_rel,
                        f"image captioned {sorted(captions)} across this book", line=h.line)
                )

    for mirror_path, parser in (
        (abs_book / ".book.yml", mirrors_fmt.parse_book_mirror),
        *[(c / ".chapter.yml", mirrors_fmt.parse_chapter_mirror) for c in chapter_dirs],
    ):
        if mirror_path.is_file():
            rel = _rel(root, mirror_path)
            text = mirror_path.read_text(encoding="utf-8", errors="replace")
            if check_mirror_digest:
                out.extend(_check_mirror(root, rel, text))
            try:
                parser(text, path=mirror_path)
            except FormatError as e:
                out.append(fail(registry.WS_MIRROR_MALFORMED, rel, e.detail or e.kind))

    return out


def _validate_wiki_page(
    root: Path, rel: str, path: Path, *, is_in_chapter: bool, cterms: list[ConfidentialTerm],
    slugs: WikiSlugs, bs_host: str | None,
) -> list[Failure]:
    out: list[Failure] = []
    raw_text, read_fail = _read_text(root, rel, default_rule=registry.WIKI_BAD_FRONTMATTER)
    if raw_text is None:
        return [read_fail] if read_fail else []
    out.extend(_check_txt(rel, raw_text, cterms))
    try:
        doc = wiki_fmt.parse(raw_text, path=path)
    except FormatError as e:
        out.append(_fmt_failure(rel, e, _WIKI_KIND_TO_RULE, registry.WIKI_BAD_FRONTMATTER))
        return out

    out.extend(wikibody.check_page(rel, raw_text, doc.body, doc.title))
    out.extend(_check_internal_links(rel, wikibody.parse_body(doc.body), slugs, bs_host))

    for ref in wiki_images(doc.body):
        expected_prefix = "../images" if is_in_chapter else "images"
        if not ref.path.startswith(expected_prefix + "/"):
            out.append(fail(registry.REF_BAD_WIKI_IMAGE_PATH, rel,
                            f"{ref.path!r}: expected the {expected_prefix}/ spelling here",
                            line=ref.line))
            continue
        book_dir = path.parent.parent if is_in_chapter else path.parent
        fname = ref.path.rsplit("/", 1)[-1]
        if not (book_dir / "images" / fname).is_file():
            out.append(fail(registry.REF_UNRESOLVED, rel, f"{ref.path!r} does not resolve",
                            line=ref.line))
    return out


# --- index consistency --------------------------------------------------------------


def _entry_kind(index: Index, rel: str) -> IndexKind | None:
    rec = index.get(rel)
    return rec.kind if rec else None


def _check_index_kind(root: Path, path: Path, index: Index, expected: IndexKind) -> list[Failure]:
    rel = _rel(root, path)
    rec = index.get(rel)
    if rec is None:
        return []
    if rec.kind is expected:
        return []
    rule_id = registry.IDX_EVIDENCE_KIND_MISMATCH
    return [fail(rule_id, rel, f"indexed as {rec.kind.value}, expected {expected.value}")]


def _is_local_only(rel: PurePosixPath) -> bool:
    """``findings/inbox/`` and ``methodology/checklists/`` — validated (see
    ``_validate_inbox_dir``/``_validate_book_dir``), but NEVER synced and therefore
    NEVER a legitimate index entry."""
    parts = rel.parts
    return (
        (len(parts) >= 2 and parts[0] == "findings" and parts[1] == "inbox")
        or (len(parts) >= 2 and parts[0] == "methodology" and parts[1] == "checklists")
    )


def _check_index_shapes(root: Path, index: Index) -> list[Failure]:
    """IDX-002/IDX-004 for every indexed path: does the recorded kind match what the
    path's own shape implies? A path under a local-only tree is a mismatch by
    construction — it can never correspond to any real remote kind."""
    out: list[Failure] = []
    for path, rec in sorted(index.records.items()):
        p = PurePosixPath(path)
        if _is_local_only(p):
            out.append(fail(registry.IDX_KIND_PATH_MISMATCH, path,
                            f"indexed as {rec.kind.value}, but this path is under a "
                            "local-only tree (findings/inbox/ or methodology/checklists/) "
                            "and must never be indexed"))
            continue
        expected = _expected_kind(p)
        if expected is None:
            continue
        if rec.kind is not expected:
            rule_id = (
                registry.IDX_EVIDENCE_KIND_MISMATCH
                if "evidence" in p.parts or "images" in p.parts
                else registry.IDX_KIND_PATH_MISMATCH
            )
            out.append(fail(rule_id, path, f"indexed as {rec.kind.value}, path shape implies "
                            f"{expected.value}"))
    return out


def _expected_kind(rel: PurePosixPath) -> IndexKind | None:
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "findings" and parts[1] == "library":
        if len(parts) == 3 and parts[2].endswith(".md"):
            return IndexKind.GW_FINDING
        return None
    if len(parts) >= 3 and parts[0] == "findings" and parts[1] == "reports":
        if len(parts) == 3:
            return IndexKind.GW_REPORT
        if len(parts) == 4 and parts[3].endswith(".md"):
            return IndexKind.GW_REPORTED_FINDING
        if len(parts) == 5 and parts[3] == "evidence":
            return IndexKind.GW_EVIDENCE
        if len(parts) == 5 and parts[3] == "notes" and parts[4].endswith(".md"):
            return IndexKind.GW_PROJECT_NOTE
        if len(parts) == 5 and parts[3] == "narrative" and parts[4].endswith(".md"):
            return IndexKind.GW_REPORT_SECTION
        return None
    if len(parts) >= 3 and parts[0] == "methodology" and parts[1] == "library":
        if parts[2] == ".shelves":
            return IndexKind.BS_SHELF
        if len(parts) == 3:
            return IndexKind.BS_BOOK
        if len(parts) == 4 and parts[3].endswith(".md"):
            return IndexKind.BS_PAGE
        if len(parts) == 4:
            return IndexKind.BS_CHAPTER
        if len(parts) == 5 and parts[3] == "images":
            return IndexKind.BS_IMAGE
        if len(parts) == 5 and parts[4].endswith(".md"):
            return IndexKind.BS_PAGE
        return None
    return None


# --- orchestration -------------------------------------------------------------------


def _check_manifest_and_hygiene(root: Path) -> list[Failure]:
    out: list[Failure] = []
    try:
        manifest_mod.check(root)
    except manifest_mod.WorkspaceNeedsMigration as e:
        out.append(fail(registry.WS_NEEDS_MIGRATION, ".grison/manifest.yml", str(e)))
    except manifest_mod.WorkspaceTooNew as e:
        out.append(fail(registry.WS_TOO_NEW, ".grison/manifest.yml", str(e)))
    except manifest_mod.ManifestError as e:
        out.append(fail(registry.WS_BAD_MANIFEST, ".grison/manifest.yml", str(e)))

    for problem in manifest_mod.check_git_hygiene(root):
        out.append(fail(registry.WS_GIT_HYGIENE, ".grison", problem))
    return out


def _load_index(root: Path) -> tuple[Index | None, list[Failure]]:
    try:
        return Index.load(root), []
    except IndexFileError as e:
        return None, [fail(registry.IDX_BAD_INDEX, ".grison/index.json", str(e))]


def _discover_report_dirs(root: Path) -> list[PurePosixPath]:
    base = root / "findings" / "reports"
    if not base.is_dir():
        return []
    return [PurePosixPath(_rel(root, d)) for d in sorted(base.iterdir()) if d.is_dir()]


def _discover_book_dirs(root: Path, rel_base: str) -> list[PurePosixPath]:
    """Every book-shaped directory directly under ``root / rel_base`` — real books
    under ``methodology/library``, or engagement copies under
    ``methodology/checklists`` (same shape, ``rel_base`` picks which)."""
    base = root / rel_base
    if not base.is_dir():
        return []
    return [
        PurePosixPath(_rel(root, d))
        for d in sorted(base.iterdir())
        if d.is_dir() and d.name != ".shelves"
    ]


def _discover_library_files(root: Path) -> list[PurePosixPath]:
    lib_dir = root / "findings" / "library"
    if not lib_dir.is_dir():
        return []
    return [PurePosixPath(_rel(root, p)) for p in sorted(lib_dir.glob("*.md"))]


def _discover_inbox_files(root: Path) -> list[PurePosixPath]:
    inbox_dir = root / "findings" / "inbox"
    if not inbox_dir.is_dir():
        return []
    return [PurePosixPath(_rel(root, p)) for p in sorted(inbox_dir.glob("*.md"))]


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
            out.append(fail(registry.WS_UNKNOWN_FINDINGS_PATH, _rel(root, p),
                            "only *.md files are allowed directly in findings/inbox/"))
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
            out.append(fail(registry.WS_UNKNOWN_FINDINGS_PATH, _rel(root, p),
                            "only *.md files are allowed directly in findings/library/"))
    return out


def _check_methodology_toplevel(root: Path) -> list[Failure]:
    out: list[Failure] = []
    methodology = root / "methodology"
    if not methodology.is_dir():
        return out
    known = {"library", "checklists"}
    for p in sorted(methodology.iterdir()):
        if p.name not in known:
            out.append(fail(registry.WS_UNKNOWN_METHODOLOGY_PATH, _rel(root, p), p.name))
    return out


def _check_shelves(root: Path) -> list[Failure]:
    """WS-003 (only ``*.yml`` files directly in ``.shelves/``) AND each shelf file's
    own schema (``WS-010`` — previously never actually wired up anywhere)."""
    out: list[Failure] = []
    shelves_dir = root / "methodology" / "library" / ".shelves"
    if not shelves_dir.is_dir():
        return out
    for p in sorted(shelves_dir.iterdir()):
        if p.is_dir() or not p.name.endswith(".yml"):
            out.append(fail(registry.WS_UNKNOWN_METHODOLOGY_PATH, _rel(root, p),
                            "only *.yml files are allowed directly in .shelves/"))
            continue
        rel = _rel(root, p)
        text = p.read_text(encoding="utf-8", errors="replace")
        out.extend(_check_mirror(root, rel, text))
        try:
            mirrors_fmt.parse_shelf_mirror(text, path=p)
        except FormatError as e:
            out.append(fail(registry.WS_MIRROR_MALFORMED, rel, e.detail or e.kind))
    return out


def _load_terms(root: Path) -> list[ConfidentialTerm]:
    return terms_mod.load_terms(root)


# --- scope resolution (§9 of the spec) -----------------------------------------------
#
# A `paths=` argument names a SCOPE, never a filter that can silently match nothing:
# every given path is resolved to real validation units (below) or the call raises
# ValidationScopeError — there is no way to spell a path that makes validate_workspace
# return an empty, "clean-looking" result for something that wasn't actually checked.


@dataclass
class _Scope:
    library_files: set[PurePosixPath] = field(default_factory=set)
    library_all: bool = False
    inbox_files: set[PurePosixPath] = field(default_factory=set)
    inbox_all: bool = False
    report_dirs: set[PurePosixPath] = field(default_factory=set)
    book_dirs: set[PurePosixPath] = field(default_factory=set)
    checklist_dirs: set[PurePosixPath] = field(default_factory=set)
    check_findings_top: bool = False
    check_library_flat: bool = False
    check_methodology_top: bool = False
    check_shelves: bool = False
    #: directory -> the exact file(s) within it to narrow this directory's own
    #: failures down to (task item 7): a single FILE argument inside a report/book/
    #: checklist directory reports only that file's own failures plus cross-file
    #: failures that involve it — every such rule (REF-003/004, IDX-003, WS-009/010,
    #: WIKI-007, …) already files its failure under the SPECIFIC document it
    #: concerns (never one shared failure for a whole directory), so "narrow to file
    #: X" is exactly "keep only failures whose own path is X". Absent from this
    #: dict, or mapped to ``None``, means unnarrowed (today's whole-directory
    #: behaviour) — a DIRECTORY argument (or "the workspace root"/"findings"/
    #: "methodology") never narrows.
    narrow: dict[PurePosixPath, frozenset[PurePosixPath] | None] = field(default_factory=dict)

    def merge(self, other: _Scope) -> None:
        self.library_files |= other.library_files
        self.library_all = self.library_all or other.library_all
        self.inbox_files |= other.inbox_files
        self.inbox_all = self.inbox_all or other.inbox_all
        self.report_dirs |= other.report_dirs
        self.book_dirs |= other.book_dirs
        self.checklist_dirs |= other.checklist_dirs
        self.check_findings_top = self.check_findings_top or other.check_findings_top
        self.check_library_flat = self.check_library_flat or other.check_library_flat
        self.check_methodology_top = self.check_methodology_top or other.check_methodology_top
        self.check_shelves = self.check_shelves or other.check_shelves
        for d, files in other.narrow.items():
            existing = self.narrow.get(d)
            if d not in self.narrow:
                self.narrow[d] = files
            elif existing is None or files is None:
                self.narrow[d] = None  # either request wants the whole directory
            else:
                self.narrow[d] = existing | files  # union: both files' own failures


def _narrowed(scope: _Scope, d: PurePosixPath, failures: list[Failure]) -> list[Failure]:
    """Apply ``scope.narrow[d]`` (task item 7) to one directory's own failure list.
    Every rule this validator fires already files its failure under the specific
    document it concerns — including the cross-file ones (REF-003/004): each
    involved document gets its OWN failure entry with its own path, never one shared
    failure for a whole directory — so "narrow to file X" is exactly "keep only
    failures whose own path is X", with no extra cross-referencing logic needed."""
    only = scope.narrow.get(d)
    if only is None:
        return failures
    wanted = {f.as_posix() for f in only}
    return [f for f in failures if f.path in wanted]


def _scope_everything(root: Path) -> _Scope:
    return _Scope(
        library_all=True,
        inbox_all=True,
        report_dirs=set(_discover_report_dirs(root)),
        book_dirs=set(_discover_book_dirs(root, "methodology/library")),
        checklist_dirs=set(_discover_book_dirs(root, "methodology/checklists")),
        check_findings_top=True,
        check_library_flat=True,
        check_methodology_top=True,
        check_shelves=True,
    )


def _scope_for_path(root: Path, parts: tuple[str, ...], *, is_dir: bool) -> _Scope:
    """``parts`` is a non-empty, workspace-relative path (the root case is handled by
    the caller). Raises :class:`ValidationScopeError` for anything that isn't findings/
    or methodology/ content — a directory outside those trees, or a path this document
    doesn't otherwise recognize, is never silently treated as "nothing to check"."""
    s = _Scope()

    if parts[0] not in ("findings", "methodology"):
        raise ValidationScopeError(
            f"{'/'.join(parts)} is not a workspace document (only findings/ and "
            "methodology/ are validated)"
        )

    if parts == ("findings",):
        s.library_all = True
        s.inbox_all = True
        s.report_dirs = set(_discover_report_dirs(root))
        s.check_findings_top = True
        s.check_library_flat = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "library":
        # a real *.md file gets narrowed to just itself; a bare "findings/library",
        # a stray subdirectory, or a non-.md entry falls back to the whole area so
        # WS-002 ("only *.md files allowed here") still fires rather than being
        # silently skipped for the one path that would have caught it.
        if not is_dir and len(parts) == 3 and parts[2].endswith(".md"):
            s.library_files = {PurePosixPath(*parts)}
        else:
            s.library_all = True
        s.check_library_flat = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "inbox":
        if not is_dir and len(parts) == 3 and parts[2].endswith(".md"):
            s.inbox_files = {PurePosixPath(*parts)}
        else:
            s.inbox_all = True
        return s

    if parts[0] == "findings" and len(parts) >= 2 and parts[1] == "reports":
        if len(parts) == 2:
            s.report_dirs = set(_discover_report_dirs(root))
        else:
            d = PurePosixPath(*parts[:3])
            s.report_dirs = {d}
            if len(parts) > 3 and not is_dir:
                # a FILE inside the report dir (task item 7): narrow to just its own
                # failures + whichever cross-file failures name it — never the whole
                # report's sibling files. A DIRECTORY argument (the report dir
                # itself, or narrative/notes/evidence as a subdirectory) is left
                # unnarrowed — "a DIRECTORY argument keeps today's behaviour".
                s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    if parts[0] == "findings":
        raise ValidationScopeError(f"{'/'.join(parts)} is not a validated workspace location")

    # methodology/…
    if parts == ("methodology",):
        s.book_dirs = set(_discover_book_dirs(root, "methodology/library"))
        s.checklist_dirs = set(_discover_book_dirs(root, "methodology/checklists"))
        s.check_methodology_top = True
        s.check_shelves = True
        return s

    if len(parts) >= 2 and parts[1] == "library":
        if len(parts) >= 3 and parts[2] == ".shelves":
            s.check_shelves = True
            return s
        if len(parts) == 2:
            s.book_dirs = set(_discover_book_dirs(root, "methodology/library"))
            s.check_shelves = True
            return s
        d = PurePosixPath(*parts[:3])
        s.book_dirs = {d}
        if len(parts) > 3 and not is_dir:
            s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    if len(parts) >= 2 and parts[1] == "checklists":
        if len(parts) == 2:
            s.checklist_dirs = set(_discover_book_dirs(root, "methodology/checklists"))
            return s
        d = PurePosixPath(*parts[:3])
        s.checklist_dirs = {d}
        if len(parts) > 3 and not is_dir:
            s.narrow[d] = frozenset({PurePosixPath(*parts)})
        return s

    raise ValidationScopeError(f"{'/'.join(parts)} is not a validated workspace location")


def _resolve_given_path(root: Path, given: Path, *, deleted_ok: bool) -> _Scope:
    ap = (given if given.is_absolute() else (Path.cwd() / given)).resolve()
    try:
        rel = ap.relative_to(root)
    except ValueError:
        raise ValidationScopeError(f"path is outside the workspace: {given}") from None
    rel_posix = PurePosixPath(rel.as_posix()) if str(rel) != "." else PurePosixPath()

    if not rel_posix.parts:  # the workspace root itself — "." / "./" / the root path
        return _scope_everything(root)

    exists = (root / rel_posix).exists()
    if not exists:
        if deleted_ok:
            parent = rel_posix.parent
            parent_abs = root / parent if parent.parts else root
            if not parent_abs.is_dir():
                raise ValidationScopeError(f"no such path: {given}")
            if not parent.parts:
                return _scope_everything(root)
            return _scope_for_path(root, parent.parts, is_dir=True)
        raise ValidationScopeError(f"no such path: {given}")

    if rel_posix.parts[0] == ".grison":
        raise ValidationScopeError(
            f"{rel_posix} is inside .grison/ (private, not a workspace document)"
        )

    return _scope_for_path(root, rel_posix.parts, is_dir=(root / rel_posix).is_dir())


def validate_workspace(
    root: Path, *, paths: list[Path] | None = None, deleted_ok: bool = False
) -> list[Failure]:
    """Validate ``root`` (a workspace) entirely, or (when ``paths`` is given) just the
    scope those paths name: a path equal to the workspace root means everything; a
    directory means everything under it plus the cross-file rules touching it
    (evidence/image stems+captions, mirrors, index consistency); a file means itself
    plus those same cross-file rules for its containing report/book/checklist
    directory. A path outside the workspace, one that doesn't exist, or one that isn't
    a validated location raises :class:`~grison.validator.errors.ValidationScopeError`
    — never silently treated as nothing to check (see the module comment above
    ``_Scope``). ``deleted_ok=True`` additionally accepts a path that no longer exists
    (e.g. the post-edit hook running after a delete) by falling back to its parent
    directory's scope.
    """
    root = root.resolve()
    out: list[Failure] = []
    out.extend(_check_manifest_and_hygiene(root))
    index, index_failures = _load_index(root)
    out.extend(index_failures)
    if index is not None:
        out.extend(_check_index_shapes(root, index))
    cterms = _load_terms(root)
    library_slugs = _collect_wiki_slugs(root / "methodology" / "library")
    bs_host = _bs_host(root)

    if paths is None:
        scope = _scope_everything(root)
    else:
        scope = _Scope()
        for given_path in paths:
            scope.merge(_resolve_given_path(root, given_path, deleted_ok=deleted_ok))

    if scope.check_findings_top:
        out.extend(_check_findings_toplevel(root))
    if scope.check_library_flat:
        out.extend(_check_library_flat(root))
    if scope.check_methodology_top:
        out.extend(_check_methodology_toplevel(root))
    if scope.check_shelves:
        out.extend(_check_shelves(root))

    if scope.library_all:
        for p in _discover_library_files(root):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "library", None, cterms))
    else:
        for p in sorted(scope.library_files):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "library", None, cterms))

    if scope.inbox_all:
        out.extend(_validate_inbox_dir(root, index, cterms))
    else:
        for p in sorted(scope.inbox_files):
            out.extend(_check_names(p))
            out.extend(_validate_finding_file(root, p.as_posix(), "inbox", None, cterms))

    for d in sorted(scope.report_dirs):
        out.extend(_narrowed(scope, d, _validate_report_dir(root, d, index, cterms)))
    for d in sorted(scope.book_dirs):
        out.extend(
            _narrowed(scope, d, _validate_book_dir(root, d, index, cterms, library_slugs, bs_host))
        )
    for d in sorted(scope.checklist_dirs):
        cslugs = _checklist_slugs(root / d, library_slugs)
        out.extend(_narrowed(scope, d, _validate_book_dir(
            root, d, None, cterms, cslugs, bs_host, check_mirror_digest=False, noun="checklist",
        )))
    return out
