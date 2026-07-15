"""BookStack methodology sync — the same 3-way reconcile as Ghostwriter, for pages.

Pages are markdown-native, so there's no converter: pull mirrors the ``markdown``
field, push PUTs it back. Structure is mirrored losslessly: the local tree is
``methodology/library/<book>/[<chapter>/]<page>.md`` (BookStack's book > chapter >
page, depth-capped), and every book and chapter — including empty ones — is
materialized as a directory with a ``.book.yml`` / ``.chapter.yml`` mirror file
(ids, name, description, shelf membership, chapter order). Mirror files are
pull-only; pages are the bidirectional surface.

Doctrine per structure level:

* **book** — identity, guarded by the structure-drift trip-wire: a remote book move
  with the local file unmoved is surfaced, never pulled (pulling would leave the file
  mis-filed and the next push would move the page back, since directory fixes book
  identity). A local book move flows as an ordinary push.
* **chapter** — content, reconciled 3-way like the body: a remote chapter move pulls
  by *relocating* the local file (no ping-pong), a local chapter move pushes as a
  parent move. Push sends a parent param (``chapter_id``/``book_id``) only when the
  local directory disagrees with the remote parent — a bare content push must never
  eject a page from its chapter.
* **priority** (sort order) and **tags** — content: remote changes pull into
  frontmatter, local edits push.

Extras BookStack needs: a post-push **literal-artifact scan** (leaked ``**`` /
``](http`` that signalled corruption during the migration). Snapshots capture each
page's pre-image *including its parent and order*, so rollback restores location;
BookStack's own revision history is the second rollback layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from grison import workspace
from grison.remote import snapshot as _snapshot  # module ref so tests can monkeypatch SNAPSHOT_ROOT
from grison.remote.bookstack import BookStackError
from grison.remote.bsmap import (
    MethPage,
    _norm_tags,
    bs_content_hash,
    is_markdown_native,
    markdown_to_page,
    page_from_record,
    page_to_markdown,
    stamp,
)

if TYPE_CHECKING:
    from grison.remote.bookstack import BookStackClient

# Literal artifacts that signal a markdown page got corrupted (from the migration lesson).
# (regex, label, blocking): leaked HTML in a markdown page is high-confidence corruption and
# refuses the push when *newly introduced*; the truncated-link pattern is a fuzzy heuristic
# (surfaced, non-blocking). A dangling-`**` check was dropped — it flags valid closing bold.
_ARTIFACT_RES = [
    (re.compile(r"\]\(https?://[^)\s]*$", re.M), "truncated link", False),
    (re.compile(r'<span class="?citation'), "leaked citation span", True),
    (re.compile(r'<div class="?notice'), "leaked notice-block div", True),
]


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _emit(on_event: Callable[[str], None] | None, msg: str) -> None:
    if on_event:
        on_event(msg)


@dataclass
class MethResult:
    pulled: list[Path] = field(default_factory=list)
    pushed: list[Path] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    repaired: list[Path] = field(default_factory=list)
    collisions: list[Path] = field(default_factory=list)
    invalid: list[Path] = field(default_factory=list)
    drift: list[tuple[Path, str]] = field(default_factory=list)
    artifacts: list[tuple[Path, str]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    moved: list[tuple[Path, Path]] = field(default_factory=list)  # pull relocations (old → new)
    materialized: list[Path] = field(default_factory=list)  # book/chapter mirrors written
    snapshot_dir: Path | None = None
    mass_change_blocked: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _BSUndo:
    op: str  # update_page | delete_page
    id: int
    markdown: str | None = None
    name: str | None = None
    book_id: int | None = None
    chapter_id: int | None = None  # 0 = book root (pre-image), None = unknown
    priority: int | None = None
    tags: list[dict] | None = None
    editor: str | None = None  # pre-image's editor — decides markdown vs html on rollback
    raw_html: str | None = None  # pre-image's html, for restoring a wysiwyg page


class _BSSnapshot:
    def __init__(self) -> None:
        self.undos: list[_BSUndo] = []

    def before_update(self, page_id: int, pre: dict) -> None:
        # Capture editor + raw html on EVERY pre-image, not just wysiwyg ones — belt
        # and suspenders so rollback can restore a wysiwyg page even if some future
        # code path ever manages to push over one despite the guard in _apply.
        self.undos.append(
            _BSUndo(
                "update_page",
                page_id,
                markdown=(pre.get("markdown") or "").strip(),
                name=pre.get("name"),
                book_id=pre.get("book_id"),
                chapter_id=pre.get("chapter_id") or 0,
                priority=pre.get("priority"),
                tags=_norm_tags(pre.get("tags") or []),
                editor=pre.get("editor"),
                raw_html=pre.get("raw_html") or pre.get("html"),
            )
        )

    def after_create(self, page_id: int) -> None:
        self.undos.append(_BSUndo("delete_page", page_id))

    @property
    def empty(self) -> bool:
        return not self.undos

    def rollback(self, client: BookStackClient) -> None:
        for u in reversed(self.undos):
            if u.op == "update_page":
                # restore content AND location — the pre-image's parent is the chapter
                # when it had one, else the book root
                parent = dict(
                    name=u.name,
                    book_id=u.book_id if not u.chapter_id else None,
                    chapter_id=u.chapter_id or None,
                    priority=u.priority,
                    tags=u.tags,
                )
                # a wysiwyg pre-image restores via the html param — the ONLY permitted
                # use of it outside this rollback path — never markdown, which would
                # regenerate (and destroy) the page's html from an empty/stale string.
                if not is_markdown_native({"editor": u.editor, "markdown": u.markdown,
                                           "raw_html": u.raw_html}):
                    client.update_page(u.id, html=u.raw_html or "", **parent)
                else:
                    client.update_page(u.id, markdown=u.markdown or "", **parent)
            elif u.op == "delete_page":
                client.delete_page(u.id)

    def persist(self, when: str) -> Path:
        out = _snapshot.SNAPSHOT_ROOT / f"bs-{when}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "bs_undo.json").write_text(
            json.dumps([asdict(u) for u in self.undos], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out


@dataclass
class _MethPlan:
    action: str  # pull | push | create | repair | collision
    path: Path  # write target (for pulls: the canonical location, post-relocation)
    page: MethPage
    old_path: Path | None = None  # pull relocation source (chapter move / migration)
    rpage: MethPage | None = None  # remote counterpart, for push parent/priority diffs
    forced: bool = False  # --force-local: skip the concurrent-edit collision guard


def _split_location(base: Path, md: Path) -> tuple[str, str | None] | str:
    """Map a page file to (book_slug, chapter_slug|None) from its directory, or return
    a skip reason. Directory depth is the structure: book/page or book/chapter/page."""
    rel = md.relative_to(base).parts
    if len(rel) < 2:
        return "page outside a book directory"
    if len(rel) > 3:
        return "nested too deep — book/chapter/page is the maximum"
    return (rel[0], rel[1] if len(rel) == 3 else None)


def _scan_local(
    root: Path, result: MethResult, *, on_event: Callable[[str], None] | None = None
) -> tuple[dict[int, tuple[Path, MethPage]], set[int]]:
    """Index local pages by ``page_id`` — two files claiming the same id is a trip-wire:
    report every one of them via ``result.skipped`` and exclude the id from the index
    entirely. The dup-id set is also returned so the remote-only pull pass (matched
    against the index) doesn't mistake an excluded id for absent-locally and re-pull it
    onto one of the very copies being held back."""
    index: dict[int, tuple[Path, MethPage]] = {}
    seen: dict[int, Path] = {}
    dups: set[int] = set()
    base = root / "methodology" / "library"
    if not base.exists():
        return index, dups
    for md in sorted(base.rglob("*.md")):
        if md.name.endswith(".remote.md"):
            continue
        loc = _split_location(base, md)
        if isinstance(loc, str):
            result.skipped.append((md, loc))
            _emit(on_event, f"skip {_rel(root, md)}: {loc}")
            continue
        try:
            page = markdown_to_page(md.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            result.errors.append(f"{md}: {e}")
            _emit(on_event, f"error {_rel(root, md)}: {e}")
            continue
        # location: methodology/library/<book>/[<chapter>/]<page>.md — dirs win over fm
        page.book, page.chapter = loc
        pid = page.page_id
        if pid is None:
            continue
        if pid in seen:
            dups.add(pid)
            result.skipped.append((md, f"duplicate page_id {pid} (also {seen[pid]})"))
            _emit(on_event, f"skip {_rel(root, md)}: duplicate page_id {pid}")
            if pid in index:  # first occurrence — report it too, then evict from the index
                first_path, _ = index.pop(pid)
                result.skipped.append((first_path, f"duplicate page_id {pid} (also {md})"))
                _emit(on_event, f"skip {_rel(root, first_path)}: duplicate page_id {pid}")
            continue
        seen[pid] = md
        index[pid] = (md, page)
    return index, dups


def _artifact_scan(
    path: Path,
    body: str,
    remote_body: str,
    result: MethResult,
    root: Path,
    *,
    on_event: Callable[[str], None] | None = None,
) -> bool:
    """Record literal-artifact hits; return True only if a *blocking* artifact is newly
    introduced (present locally, absent in the remote baseline) — pre-existing cruft is
    surfaced but must not block a legitimate edit."""
    block = False
    for rx, label, blocking in _ARTIFACT_RES:
        if rx.search(body):
            result.artifacts.append((path, label))
            _emit(on_event, f"artifact {_rel(root, path)}: {label}")
            if blocking and not rx.search(remote_body):
                block = True
    return block


_MIRROR_HEADER = "# READ-ONLY mirror — regenerated every sync; edits here are discarded\n"


def _mirror_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_mirrors(root: Path) -> dict[str, str]:
    path = workspace.mirrors_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_mirrors(root: Path, mirrors: dict[str, str]) -> None:
    path = workspace.mirrors_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mirrors, indent=2, sort_keys=True), encoding="utf-8")


def _write_mirror(
    path: Path,
    meta: dict,
    result: MethResult,
    root: Path,
    mirrors: dict[str, str],
    *,
    dry_run: bool,
    on_event: Callable[[str], None] | None,
) -> None:
    """Render + write a book/chapter/shelf mirror — but these files are read-only
    (grison never pushes book/chapter entities): a hand-edit must never be silently
    clobbered by the next sync's regenerate. ``mirrors`` records the sha256 of the
    exact bytes grison last wrote per path (``.grison/mirrors.json``); on-disk bytes
    that no longer match that hash mean the user edited the file — keep it untouched,
    write the fresh remote render to a ``.remote.yml`` sidecar instead, and surface an
    error. No recorded hash (first materialization) always writes normally."""
    text = _MIRROR_HEADER + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    key = _rel(root, path)
    recorded = mirrors.get(key)
    if path.exists():
        on_disk = path.read_text(encoding="utf-8")
        if recorded is not None and _mirror_hash(on_disk) != recorded:
            sidecar = path.with_suffix(".remote.yml")
            wrote = "would write" if dry_run else "wrote"
            msg = (
                f"{path} was hand-edited but mirrors are read-only (regenerated every "
                f"sync) — {wrote} fresh remote state to {sidecar.name}; move edits to "
                "BookStack instead"
            )
            result.errors.append(msg)
            _emit(on_event, f"error {_rel(root, path)}: mirror hand-edited, kept local copy")
            if not dry_run:
                sidecar.write_text(text, encoding="utf-8")
            return
        if on_disk == text:
            return  # unchanged — nothing to (re)materialize
    result.materialized.append(path)
    if dry_run:
        _emit(on_event, f"would materialize {_rel(root, path.parent)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    mirrors[key] = _mirror_hash(text)
    _emit(on_event, f"materialize {_rel(root, path.parent)}")


def _materialize_structure(  # noqa: PLR0913
    root: Path,
    client: BookStackClient,
    books_list: list[dict],
    chapters_list: list[dict],
    books: dict[int, str],
    result: MethResult,
    mirrors: dict[str, str],
    *,
    dry_run: bool,
    on_event: Callable[[str], None] | None,
) -> None:
    """Mirror the shelf/book/chapter tree itself, independent of pages — a book or
    chapter with no pages still gets its directory and mirror file. Mirror files are
    pull-only (grison never pushes book/chapter entities)."""
    base = root / "methodology" / "library"
    # A shelf spans books, so it can't nest under one book dir: shelves get their own
    # mirror under library/.shelves/<slug>.yml, carrying the shelf's name/description
    # and its books IN ORDER (BookStack's books array is the authoritative shelf order —
    # sorting it, as the first cut did, discards that ordering). Each book still records
    # which shelves it belongs to, in shelf-fetch order (never alphabetized).
    shelf_map: dict[int, list[str]] = {}
    try:
        for shelf in client.fetch_shelves():
            detail = client.fetch_shelf(shelf["id"])
            ordered_books = [b.get("slug") or str(b["id"]) for b in detail.get("books") or []]
            for b in detail.get("books") or []:
                shelf_map.setdefault(b["id"], []).append(shelf["slug"])
            shelf_meta = {
                "grison": {"kind": "shelf", "bs": {"shelf_id": shelf["id"]}},
                "name": detail.get("name") or shelf.get("name", ""),
                "slug": shelf["slug"],
                "description": detail.get("description") or "",
                "books": ordered_books,
            }
            _write_mirror(base / ".shelves" / f"{shelf['slug']}.yml", shelf_meta, result, root,
                          mirrors, dry_run=dry_run, on_event=on_event)
    except BookStackError as e:
        _emit(on_event, f"shelves unavailable ({e}) — book mirrors written without shelves")
    for b in books_list:
        meta: dict = {
            "grison": {"kind": "book", "bs": {"book_id": b["id"]}},
            "name": b.get("name", ""),
            "slug": b["slug"],
            "description": b.get("description") or "",
        }
        if shelf_map.get(b["id"]):
            meta["shelves"] = shelf_map[b["id"]]
        _write_mirror(base / b["slug"] / ".book.yml", meta, result, root,
                      mirrors, dry_run=dry_run, on_event=on_event)
    for c in chapters_list:
        book_slug = books.get(c["book_id"])
        if book_slug is None:
            continue
        meta = {
            "grison": {"kind": "chapter", "bs": {"chapter_id": c["id"], "book_id": c["book_id"]}},
            "name": c.get("name", ""),
            "slug": c["slug"],
            "description": c.get("description") or "",
        }
        if c.get("priority") is not None:
            meta["priority"] = c["priority"]
        _write_mirror(base / book_slug / c["slug"] / ".chapter.yml", meta, result, root,
                      mirrors, dry_run=dry_run, on_event=on_event)


def _pull_target(root: Path, path: Path, rpage: MethPage) -> Path:
    """Canonical location for a pull: the directory follows the remote book/chapter,
    the filename stays (a filename is cosmetic, same doctrine as renames)."""
    d = root / "methodology" / "library" / rpage.book
    if rpage.chapter:
        d = d / rpage.chapter
    return d / path.name


def sync_methodology(
    root: Path,
    client: BookStackClient,
    *,
    dry_run: bool = False,
    force_local: set[Path] | None = None,
    force_remote: set[Path] | None = None,
    mass_change_ratio: float = 0.2,
    on_event: Callable[[str], None] | None = None,
) -> MethResult:
    """Reconcile methodology/library/ with BookStack (3-way per page)."""
    force_local = force_local or set()
    force_remote = force_remote or set()
    result = MethResult()
    now = datetime.now(UTC)
    lib_root = root / "methodology" / "library"
    # Snapshot BEFORE _materialize_structure recreates every remote book's directory
    # below — the book-rename tripwire needs to know whether a book's dir was ALREADY
    # gone (a real `mv <old-dir> <new-dir>`) vs merely page-less (materialize would
    # otherwise make every remote book's dir "exist" again regardless, masking it).
    pre_existing_dirs = (
        {d.name for d in lib_root.iterdir() if d.is_dir()} if lib_root.exists() else set()
    )
    mirrors = _load_mirrors(root)

    _emit(on_event, "pulling bookstack state…")
    books_list = client.fetch_books()
    books = {b["id"]: b["slug"] for b in books_list}
    slug_to_book_id = {b["slug"]: b["id"] for b in books_list}
    chapters_list = client.fetch_chapters()
    chap_slug_by_id = {c["id"]: c["slug"] for c in chapters_list}
    chap_ids = {(c["book_id"], c["slug"]): c["id"] for c in chapters_list}
    remote: dict[int, MethPage] = {}
    remote_loc: dict[int, tuple[str, str | None, str]] = {}  # pid -> (book, chapter, page slug)
    wysiwyg: dict[int, str] = {}  # pid -> description, for pages grison must never mirror
    pages_list = client.fetch_pages()
    for item in pages_list:
        detail = client.fetch_page(item["id"])
        book_slug = books.get(item["book_id"], item.get("book_slug", "book"))
        cid = detail.get("chapter_id") or 0
        chapter_slug = chap_slug_by_id.get(cid) if cid else None
        remote_loc[item["id"]] = (book_slug, chapter_slug, item["slug"])
        if not is_markdown_native(detail):
            editor = detail.get("editor") or "wysiwyg"
            wysiwyg[item["id"]] = f"id={item['id']}, name={detail.get('name')!r}, editor={editor}"
            continue
        remote[item["id"]] = page_from_record(detail, book_slug=book_slug,
                                              chapter_slug=chapter_slug)
    _emit(
        on_event,
        f"bookstack: {len(books_list)} books, {len(chapters_list)} chapters, "
        f"{len(pages_list)} pages",
    )

    _materialize_structure(root, client, books_list, chapters_list, books, result, mirrors,
                           dry_run=dry_run, on_event=on_event)
    if not dry_run:
        _save_mirrors(root, mirrors)

    local, dup_pids = _scan_local(root, result, on_event=on_event)
    _emit(on_event, f"reconciling {len(local)} pages…")

    # wysiwyg pages are never mirrored — no empty-body stub written, and one already
    # (wrongly, pre-fix) sitting on disk is never pulled-over — just a loud, durable
    # skip until the page is converted to markdown in BookStack.
    for pid, desc in wysiwyg.items():
        if pid in local:
            wpath = local[pid][0]
        else:
            book_slug, chapter_slug, page_slug = remote_loc[pid]
            wpath = lib_root / book_slug
            if chapter_slug:
                wpath = wpath / chapter_slug
            wpath = wpath / f"{page_slug}.md"
        msg = f"wysiwyg page ({desc}) — convert to markdown in BookStack before grison can sync it"
        result.skipped.append((wpath, msg))
        _emit(on_event, f"skip {_rel(root, wpath)}: {msg}")

    snap = _BSSnapshot()

    planned_writes = 0
    plans: list[_MethPlan] = []

    def _plan_pull(path: Path, rpage: MethPage) -> None:
        """Queue a pull at the canonical location; relocating onto an unrelated file
        is a trip-wire, never an overwrite."""
        target = _pull_target(root, path, rpage)
        if target != path and target.exists():
            result.skipped.append(
                (path, f"relocation target exists: {_rel(root, target)} — resolve manually")
            )
            _emit(on_event, f"skip {_rel(root, path)}: relocation target exists")
            return
        plans.append(_MethPlan("pull", target, rpage,
                               old_path=path if target != path else None))

    for pid, (path, lpage) in local.items():
        if pid in wysiwyg:
            continue  # already reported above — never a candidate for push/pull
        # Book-rename tripwire (bs-structure F7): the local dir now names a DIFFERENT
        # existing remote book than the one this page is still stamped under, and the
        # OLD book's directory is genuinely gone (not just page-less — see
        # pre_existing_dirs above) — that's `mv <bookdir> <bookdir>`, not a per-page
        # move. grison has no update_book/rename-book call; never silently mass-reparent
        # onto an unrelated book that happens to share the new directory's slug.
        old_book_slug = books.get(lpage.book_id)
        new_book_id = slug_to_book_id.get(lpage.book)
        if (
            old_book_slug is not None
            and lpage.book != old_book_slug
            and new_book_id is not None
            and new_book_id != lpage.book_id
            and old_book_slug not in pre_existing_dirs
        ):
            msg = (
                f"looks like a book rename ({old_book_slug} → {lpage.book}) — grison "
                "cannot rename books; rename the book in BookStack or restore the "
                "directory name"
            )
            result.errors.append(f"{path}: {msg}")
            _emit(on_event, f"error {_rel(root, path)}: {msg}")
            continue
        rpage = remote.get(pid)
        if force_remote and path in force_remote and rpage is not None:
            _plan_pull(path, rpage)
            continue
        if force_local and path in force_local and rpage is not None:
            plans.append(_MethPlan("push", path, lpage, rpage=rpage, forced=True))
            planned_writes += 1
            continue
        if lpage.synced_hash is None:
            result.invalid.append(path)
            _emit(on_event, f"broken link {_rel(root, path)}")
            continue
        if rpage is None:
            result.skipped.append((path, "remote page gone (orphan)"))
            _emit(on_event, f"skip {_rel(root, path)}: remote page gone (orphan)")
            continue
        # structure drift: local dir = BOOK identity. A page's filename is cosmetic
        # (same doctrine as findings — a remote rename is just a title change, pulled
        # as ordinary content) and a chapter is content (it flows as pull-relocation /
        # push-move), so only a BOOK move matters here. Drift fires only when the
        # remote book moved AND the local file hasn't followed: pulling into the file
        # sitting in the old book dir would make the next push move the page back on
        # BookStack (dir wins) — ping-pong. A local book move with BS unmoved is not
        # drift; book_id stays the witness, and it flows as an ordinary push.
        book_slug, chapter_slug, page_slug = remote_loc[pid]
        if rpage.book_id != lpage.book_id and lpage.book != book_slug:
            where = f"{book_slug}/{chapter_slug}/{page_slug}" if chapter_slug \
                else f"{book_slug}/{page_slug}"
            why = (
                f"moved on BookStack → {where} — move the file to match, "
                "or --force-local to move it back"
            )
            result.drift.append((path, why))
            _emit(on_event, f"structure-drift {_rel(root, path)}: {why}")
            continue
        base = lpage.synced_hash
        lh = bs_content_hash(lpage)
        rh = bs_content_hash(rpage)
        if lh == base and rh == base:
            result.unchanged.append(path)
        elif lh != base and rh == base:
            plans.append(_MethPlan("push", path, lpage, rpage=rpage))
            planned_writes += 1
        elif lh == base and rh != base:
            _plan_pull(path, rpage)
        elif lh == rh:
            # local == remote regardless of base: either the user moved the file to
            # match a remote move, or `base` is stale purely because bs_content_hash's
            # schema changed underneath it (new field added) — either way there's
            # nothing to reconcile, just restamp clean instead of a phantom collision.
            lpage.book_id = rpage.book_id
            lpage.chapter_id = rpage.chapter_id
            plans.append(_MethPlan("repair", path, lpage))
        else:
            plans.append(_MethPlan("collision", path, rpage))

    for pid, rpage in remote.items():
        if pid in local or pid in dup_pids:
            continue
        book_slug, chapter_slug, page_slug = remote_loc[pid]
        new_dir = root / "methodology" / "library" / book_slug
        if chapter_slug:
            new_dir = new_dir / chapter_slug
        plans.append(_MethPlan("pull", new_dir / f"{page_slug}.md", rpage))

    # new local pages (no page_id) → create
    base_dir = root / "methodology" / "library"
    if base_dir.exists():
        for md in sorted(base_dir.rglob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            loc = _split_location(base_dir, md)
            if isinstance(loc, str):  # already trip-wired in _scan_local
                continue
            try:
                lpage = markdown_to_page(md.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if lpage.page_id is None:
                lpage.book, lpage.chapter = loc
                plans.append(_MethPlan("create", md, lpage))
                planned_writes += 1

    total = max(len(remote), 1)
    if not dry_run and planned_writes > 5 and planned_writes > mass_change_ratio * total:
        result.mass_change_blocked = True
        plans = [p for p in plans if p.action not in ("push", "create")]

    # Persist the snapshot even if a page fails mid-batch; isolate per-page failures.
    try:
        for plan in plans:
            try:
                _apply(plan, client, snap, result, books, chap_ids, now, root,
                       dry_run=dry_run, on_event=on_event)
            except Exception as e:  # noqa: BLE001 — isolate one page, keep batch + snapshot
                result.errors.append(f"{plan.path}: {e}")
                _emit(on_event, f"error {_rel(root, plan.path)}: {e}")
    finally:
        if not dry_run and not snap.empty:
            result.snapshot_dir = snap.persist(now.strftime("%Y%m%dT%H%M%SZ"))
            _emit(on_event, f"snapshot → {_rel(root, result.snapshot_dir)}")
    return result


def _parent_move(
    page: MethPage, pre: dict, bid: int, chap_ids: dict
) -> tuple[int | None, int | None]:
    """The parent param (book_id, chapter_id) a push must send — at most one, and only
    when the local directory disagrees with the remote parent. A bare content push must
    never move the page; in particular it must never eject it from its chapter."""
    cur_book = pre.get("book_id")
    cur_chapter = pre.get("chapter_id") or 0
    if page.chapter:  # file sits in a chapter dir → that chapter is the intent
        cid = chap_ids.get((bid, page.chapter))
        if cid is None:
            raise ValueError(
                f"unknown chapter '{page.chapter}' in book '{page.book}' — "
                "create the chapter in BookStack first"
            )
        return (None, cid) if cid != cur_chapter else (None, None)
    if bid != cur_book or cur_chapter:
        return (bid, None)  # deliberate move to this book's root
    return (None, None)


def _remote_drifted(local_val: object, pre_val: object, base_val: object) -> bool:
    """True when the fresh pre-image disagrees with the merge base (something changed
    on BookStack since this batch's up-front snapshot) AND sending ``local_val`` would
    actually overwrite that fresh value — i.e. pushing would clobber a concurrent
    remote edit this run never even saw. Covers both ``base==local≠pre`` (local didn't
    touch the field, remote did) and both sides diverging to different values; excludes
    the safe case where local's value already matches what's now on remote."""
    return pre_val != base_val and local_val != pre_val


def _apply(  # noqa: PLR0913
    plan: _MethPlan, client, snap, result: MethResult, books: dict[int, str],
    chap_ids: dict, now: datetime, root: Path, *, dry_run: bool,
    on_event: Callable[[str], None] | None = None,
) -> None:
    # Every branch below appends to its result list only after its primary write
    # (client call, or filesystem write when there's no client call) succeeds — a
    # raised exception must leave the page uncounted, not double-counted alongside
    # the error. dry_run is the exception: it appends and returns before any write.
    action, path, page = plan.action, plan.path, plan.page
    if action == "repair":
        if dry_run:
            result.repaired.append(path)
            _emit(on_event, f"would repair {_rel(root, path)}")
            return
        stamp(page, now=now)
        path.write_text(page_to_markdown(page), encoding="utf-8")
        result.repaired.append(path)
        _emit(on_event, f"repair {_rel(root, path)}")
    elif action == "collision":
        if dry_run:
            result.collisions.append(path)
            _emit(on_event, f"would collision {_rel(root, path)}")
            return
        path.with_suffix(".remote.md").write_text(page_to_markdown(page), encoding="utf-8")
        result.collisions.append(path)
        _emit(on_event, f"collision {_rel(root, path)} → sidecar written")
    elif action == "pull":
        if dry_run:
            result.pulled.append(path)
            if plan.old_path is not None:
                result.moved.append((plan.old_path, path))
                _emit(on_event, f"would pull {_rel(root, plan.old_path)} → {_rel(root, path)}")
            else:
                _emit(on_event, f"would pull {_rel(root, path)}")
            return
        stamp(page, now=now)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page_to_markdown(page), encoding="utf-8")
        if plan.old_path is not None:
            plan.old_path.unlink(missing_ok=True)
            result.moved.append((plan.old_path, path))
            _emit(on_event, f"pull {_rel(root, plan.old_path)} → {_rel(root, path)}")
        else:
            _emit(on_event, f"pull {_rel(root, path)}")
        result.pulled.append(path)
    elif action == "push":
        pre = {} if dry_run else client.fetch_page(page.page_id)
        remote_body = (pre.get("markdown") or "").strip()
        if _artifact_scan(path, page.body, remote_body, result, root,
                           on_event=on_event) and not dry_run:
            note = "refused: new corruption artifact introduced in body"
            result.skipped.append((path, note))
            _emit(on_event, f"skip {_rel(root, path)}: {note}")
            return
        if dry_run:
            result.pushed.append(path)
            _emit(on_event, f"would push {_rel(root, path)}")
            return
        if not is_markdown_native(pre):
            # the page turned wysiwyg on BookStack since we last pulled it (editor
            # switch, or a revision restore) — sending markdown here would regenerate
            # (and permanently destroy) its html. Refuse, never update_page(markdown=…).
            note = ("refused: remote page is no longer markdown-native (wysiwyg) — "
                    "grison never pushes markdown over wysiwyg-authored content")
            result.skipped.append((path, note))
            _emit(on_event, f"skip {_rel(root, path)}: {note}")
            return
        bid = _book_id_for(books, page.book)
        if bid is None:
            msg = f"unknown book '{page.book}' — create the book in BookStack first"
            result.errors.append(f"{path}: {msg}")
            _emit(on_event, f"error {_rel(root, path)}: {msg}")
            return
        pre_tags = _norm_tags(pre.get("tags") or [])
        # Concurrent-edit guard: priority already re-validated against a fresh
        # pre-image before sending (below) — name/tags did not, so a rename/retag
        # that happened on BookStack *during this batch* (after the up-front remote
        # snapshot, before this page's own apply step) was silently overwritten.
        # `plan.rpage` is that up-front snapshot, which by construction equals the
        # merge base for this page (rh == base was required to reach a push plan) —
        # so it doubles as the per-field "base" for a real 3-way diff against `pre`.
        # `--force-local` explicitly asks to push local regardless of remote drift,
        # so it skips this guard; it never skips the wysiwyg guard above.
        if not plan.forced and plan.rpage is not None:
            base = plan.rpage
            if (
                _remote_drifted(page.title, pre.get("name"), base.title)
                or _remote_drifted(page.tags, pre_tags, base.tags)
                or _remote_drifted(page.priority, pre.get("priority"), base.priority)
            ):
                fresh_book = books.get(pre.get("book_id"), page.book)
                fresh = page_from_record(pre, book_slug=fresh_book, chapter_slug=page.chapter)
                path.with_suffix(".remote.md").write_text(page_to_markdown(fresh),
                                                           encoding="utf-8")
                result.collisions.append(path)
                _emit(on_event, f"collision {_rel(root, path)} → name/tags/priority "
                                "changed on BookStack mid-sync")
                return
        move_book, move_chapter = _parent_move(page, pre, bid, chap_ids)
        prio = page.priority if page.priority != pre.get("priority") else None
        name_val = page.title if page.title != pre.get("name") else None
        tags_val = page.tags if page.tags != pre_tags else None
        snap.before_update(page.page_id, pre)
        client.update_page(page.page_id, markdown=page.body, name=name_val,
                           book_id=move_book, chapter_id=move_chapter, priority=prio,
                           tags=tags_val)
        result.pushed.append(path)
        _emit(on_event, f"push {_rel(root, path)}")
        # keep frontmatter truthful — the drift witnesses depend on it
        page.book_id = bid
        if move_chapter is not None:
            page.chapter_id = move_chapter
        elif move_book is not None:
            page.chapter_id = 0
        else:
            page.chapter_id = pre.get("chapter_id") or 0
        stamp(page, now=now)
        path.write_text(page_to_markdown(page), encoding="utf-8")
    elif action == "create":
        if _artifact_scan(path, page.body, "", result, root, on_event=on_event):
            note = "refused: corruption artifact in new page body"
            result.skipped.append((path, note))
            _emit(on_event, f"skip {_rel(root, path)}: {note}")
            return
        if dry_run:
            result.created.append(path)
            _emit(on_event, f"would create {_rel(root, path)}")
            return
        book_id = _book_id_for(books, page.book)
        if book_id is None:
            msg = f"{path}: unknown book '{page.book}' — create the book in BookStack first"
            result.errors.append(msg)
            _emit(on_event, f"error {_rel(root, path)}: unknown book '{page.book}'")
            return
        chapter_id = None
        if page.chapter:
            chapter_id = chap_ids.get((book_id, page.chapter))
            if chapter_id is None:
                msg = (f"{path}: unknown chapter '{page.chapter}' in book '{page.book}' — "
                       "create the chapter in BookStack first")
                result.errors.append(msg)
                _emit(on_event, f"error {_rel(root, path)}: unknown chapter '{page.chapter}'")
                return
        rec = client.create_page(name=page.title, markdown=page.body,
                                 book_id=book_id, chapter_id=chapter_id, tags=page.tags,
                                 priority=page.priority)
        result.created.append(path)
        _emit(on_event, f"create {_rel(root, path)}")
        snap.after_create(rec["id"])
        page.page_id = rec["id"]
        page.book_id = rec.get("book_id", book_id)
        page.chapter_id = rec.get("chapter_id") or (chapter_id or 0)
        page.priority = rec.get("priority")
        stamp(page, now=now)
        path.write_text(page_to_markdown(page), encoding="utf-8")


def _book_id_for(books: dict[int, str], slug: str) -> int | None:
    for bid, bslug in books.items():
        if bslug == slug:
            return bid
    return None
