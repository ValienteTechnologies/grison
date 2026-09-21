"""``methodology/library/<book>`` (and, via the same shape,
``methodology/checklists/<engagement>``) directory and page validation."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from grison.engine.sidecar import is_sidecar_name
from grison.formats import mirrors as mirrors_fmt
from grison.formats import wiki as wiki_fmt
from grison.formats.common import FormatError
from grison.index import Index, IndexKind
from grison.validator import registry, wikibody
from grison.validator.core.common import (
    _WIKI_KIND_TO_RULE,
    _check_mirror,
    _check_txt,
    _EmbedHit,
    _fmt_failure,
    _read_text,
    _rel,
)
from grison.validator.core.index_rules import _check_index_kind
from grison.validator.core.names import _check_fileset_name, _check_names
from grison.validator.core.wiki_slugs import WikiSlugs, _check_internal_links
from grison.validator.refs import stems, wiki_images
from grison.validator.registry import Failure, fail
from grison.validator.terms import ConfidentialTerm


def _validate_book_dir(
    root: Path,
    book_dir: PurePosixPath,
    index: Index | None,
    cterms: list[ConfidentialTerm],
    slugs: WikiSlugs,
    bs_host: str | None,
    *,
    check_mirror_digest: bool = True,
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
        out.append(
            fail(
                registry.WS_UNKNOWN_METHODOLOGY_PATH,
                _rel(root, p),
                f"unexpected entry directly inside a {noun} directory",
            )
        )
    for cdir in chapter_dirs:
        out.extend(_check_names(PurePosixPath(_rel(root, cdir))))
        page_files.extend(sorted(cdir.glob("*.md")))
        for p in sorted(cdir.iterdir()):
            if p.name == ".chapter.yml" or (p.is_file() and p.name.endswith(".md")):
                continue
            out.append(
                fail(
                    registry.WS_UNKNOWN_METHODOLOGY_PATH,
                    _rel(root, p),
                    "unexpected entry directly inside a chapter directory",
                )
            )

    embed_hits: list[_EmbedHit] = []
    for page in page_files:
        rel = _rel(root, page)
        out.extend(_check_names(PurePosixPath(rel)))
        chapter = page.parent != abs_book
        out.extend(
            _validate_wiki_page(
                root, rel, page, is_in_chapter=chapter, cterms=cterms, slugs=slugs, bs_host=bs_host
            )
        )
        text = page.read_text(encoding="utf-8", errors="replace")
        try:
            doc = wiki_fmt.parse(text, path=page)
        except FormatError:
            continue
        for ref in wiki_images(doc.body):
            embed_hits.append(_EmbedHit(ref.path, ref.path, ref.caption, rel, ref.line))

    if images_dir.is_dir():
        # Same sidecar exclusion as the evidence scan above (item 5) — a
        # `<name>.remote.<ext>` collision sidecar is never a gallery image, so
        # it's excluded from REF-003/index-kind checking; its OWN name is still
        # checked below (REF-008 rejects the sidecar shape outright).
        filenames = sorted(
            p.name for p in images_dir.iterdir() if p.is_file() and not is_sidecar_name(p.name)
        )
        for stem, group in stems(filenames).items():
            if len(group) > 1:
                for name in group:
                    out.append(
                        fail(
                            registry.REF_STEM_COLLISION,
                            _rel(root, images_dir / name),
                            f"shares stem {stem!r} with {[g for g in group if g != name]}",
                        )
                    )
        images_dir_rel = PurePosixPath(_rel(root, images_dir))
        for p in sorted(images_dir.iterdir()):
            if p.is_file():
                out.extend(_check_names(PurePosixPath(_rel(root, p)), skip_last=True))
                out.extend(_check_fileset_name(images_dir_rel, p.name))
                if not is_sidecar_name(p.name) and index is not None:
                    out.extend(_check_index_kind(root, p, index, IndexKind.BS_IMAGE))

    by_ref: dict[str, list[_EmbedHit]] = {}
    for hit in embed_hits:
        by_ref.setdefault(hit.ref.rsplit("/", 1)[-1], []).append(hit)
    for _fname, hits in by_ref.items():
        captions = {h.caption for h in hits if h.caption.strip()}
        if len(captions) > 1:
            for h in hits:
                out.append(
                    fail(
                        registry.REF_CAPTION_CONFLICT,
                        h.doc_rel,
                        f"image captioned {sorted(captions)} across this book",
                        line=h.line,
                    )
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
    root: Path,
    rel: str,
    path: Path,
    *,
    is_in_chapter: bool,
    cterms: list[ConfidentialTerm],
    slugs: WikiSlugs,
    bs_host: str | None,
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
            out.append(
                fail(
                    registry.REF_BAD_WIKI_IMAGE_PATH,
                    rel,
                    f"{ref.path!r}: expected the {expected_prefix}/ spelling here",
                    line=ref.line,
                )
            )
            continue
        book_dir = path.parent.parent if is_in_chapter else path.parent
        fname = ref.path.rsplit("/", 1)[-1]
        if not (book_dir / "images" / fname).is_file():
            out.append(
                fail(registry.REF_UNRESOLVED, rel, f"{ref.path!r} does not resolve", line=ref.line)
            )
    return out
