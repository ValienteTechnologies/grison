"""BookStack methodology sync — the same 3-way reconcile as Ghostwriter, for pages.

Pages are markdown-native, so there's no converter: pull mirrors the ``markdown``
field, push PUTs it back. Extras BookStack needs: a **structure-drift** trip-wire
(the page's book moved on BookStack while the local file stayed in the old book's
directory — pulling here would leave the file mis-filed, and the next push would
move the page back on BookStack, since directory fixes book identity; surface,
don't pull) and a post-push **literal-artifact scan** (leaked ``**`` / ``](http``
that signalled corruption during the migration). Snapshots capture each page's
pre-image; BookStack's own revision history is the second rollback layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from grison.remote import snapshot as _snapshot  # module ref so tests can monkeypatch SNAPSHOT_ROOT
from grison.remote.bsmap import (
    MethPage,
    bs_content_hash,
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
    snapshot_dir: Path | None = None
    mass_change_blocked: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class _BSUndo:
    op: str  # update_page | delete_page
    id: int
    markdown: str | None = None


class _BSSnapshot:
    def __init__(self) -> None:
        self.undos: list[_BSUndo] = []

    def before_update(self, page_id: int, pre_markdown: str) -> None:
        self.undos.append(_BSUndo("update_page", page_id, pre_markdown))

    def after_create(self, page_id: int) -> None:
        self.undos.append(_BSUndo("delete_page", page_id))

    @property
    def empty(self) -> bool:
        return not self.undos

    def rollback(self, client: BookStackClient) -> None:
        for u in reversed(self.undos):
            if u.op == "update_page":
                client.update_page(u.id, markdown=u.markdown or "")
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
        try:
            page = markdown_to_page(md.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            result.errors.append(f"{md}: {e}")
            _emit(on_event, f"error {_rel(root, md)}: {e}")
            continue
        # location: methodology/library/<book-slug>/<page-slug>.md
        page.book = md.parent.name
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

    _emit(on_event, "pulling bookstack state…")
    books_list = client.fetch_books()
    books = {b["id"]: b["slug"] for b in books_list}
    remote: dict[int, MethPage] = {}
    remote_slug: dict[int, tuple[str, str]] = {}  # page_id -> (book_slug, page_slug)
    pages_list = client.fetch_pages()
    for item in pages_list:
        detail = client.fetch_page(item["id"])
        book_slug = books.get(item["book_id"], item.get("book_slug", "book"))
        remote[item["id"]] = page_from_record(detail, book_slug=book_slug)
        remote_slug[item["id"]] = (book_slug, item["slug"])
    _emit(on_event, f"bookstack: {len(books_list)} books, {len(pages_list)} pages")

    local, dup_pids = _scan_local(root, result, on_event=on_event)
    _emit(on_event, f"reconciling {len(local)} pages…")
    snap = _BSSnapshot()

    planned_writes = 0
    plans: list[tuple[str, Path, MethPage]] = []

    for pid, (path, lpage) in local.items():
        rpage = remote.get(pid)
        if force_remote and path in force_remote and rpage is not None:
            plans.append(("pull", path, rpage))
            continue
        if force_local and path in force_local and rpage is not None:
            plans.append(("push", path, lpage))
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
        # structure drift: local dir = book identity. A page's filename is cosmetic
        # (same doctrine as findings — a remote rename is just a title change, pulled
        # as ordinary content) so only a BOOK move matters. Drift fires only when the
        # remote book moved AND the local file hasn't followed: pulling into the file
        # sitting in the old book dir would make the next push move the page back on
        # BookStack (dir wins) — ping-pong. A local book move with BS unmoved is not
        # drift; book_id stays the witness, and it flows as an ordinary push.
        book_slug, page_slug = remote_slug[pid]
        if rpage.book_id != lpage.book_id and path.parent.name != book_slug:
            why = (
                f"moved on BookStack → {book_slug}/{page_slug} — move the file to match, "
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
            plans.append(("push", path, lpage))
            planned_writes += 1
        elif lh == base and rh != base:
            plans.append(("pull", path, rpage))
        elif lh == rh:
            lpage.book_id = rpage.book_id  # user moved the file to match a remote book move
            plans.append(("repair", path, lpage))
        else:
            plans.append(("collision", path, rpage))

    for pid, rpage in remote.items():
        if pid in local or pid in dup_pids:
            continue
        book_slug, page_slug = remote_slug[pid]
        new_path = root / "methodology" / "library" / book_slug / f"{page_slug}.md"
        plans.append(("pull", new_path, rpage))

    # new local pages (no page_id) → create
    base_dir = root / "methodology" / "library"
    if base_dir.exists():
        for md in sorted(base_dir.rglob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            try:
                lpage = markdown_to_page(md.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if lpage.page_id is None:
                lpage.book = md.parent.name
                plans.append(("create", md, lpage))
                planned_writes += 1

    total = max(len(remote), 1)
    if not dry_run and planned_writes > 5 and planned_writes > mass_change_ratio * total:
        result.mass_change_blocked = True
        plans = [p for p in plans if p[0] not in ("push", "create")]

    # Persist the snapshot even if a page fails mid-batch; isolate per-page failures.
    try:
        for action, path, page in plans:
            try:
                _apply(action, path, page, client, snap, result, books, now, root,
                       dry_run=dry_run, on_event=on_event)
            except Exception as e:  # noqa: BLE001 — isolate one page, keep batch + snapshot
                result.errors.append(f"{path}: {e}")
                _emit(on_event, f"error {_rel(root, path)}: {e}")
    finally:
        if not dry_run and not snap.empty:
            result.snapshot_dir = snap.persist(now.strftime("%Y%m%dT%H%M%SZ"))
            _emit(on_event, f"snapshot → {_rel(root, result.snapshot_dir)}")
    return result


def _apply(  # noqa: ANN001, PLR0913
    action, path, page, client, snap, result, books, now, root, *, dry_run,
    on_event: Callable[[str], None] | None = None,
):
    # Every branch below appends to its result list only after its primary write
    # (client call, or filesystem write when there's no client call) succeeds — a
    # raised exception must leave the page uncounted, not double-counted alongside
    # the error. dry_run is the exception: it appends and returns before any write.
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
            _emit(on_event, f"would pull {_rel(root, path)}")
            return
        stamp(page, now=now)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page_to_markdown(page), encoding="utf-8")
        result.pulled.append(path)
        _emit(on_event, f"pull {_rel(root, path)}")
    elif action == "push":
        remote_body = "" if dry_run else _remote_markdown(client, page.page_id)
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
        snap.before_update(page.page_id, remote_body)
        bid = _book_id_for(books, page.book)
        # push title + book too, so a local rename/move actually reaches BookStack
        client.update_page(page.page_id, markdown=page.body, name=page.title, book_id=bid)
        result.pushed.append(path)
        _emit(on_event, f"push {_rel(root, path)}")
        page.book_id = bid  # keep frontmatter truthful — the drift witness depends on it
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
        new_id = client.create_page(book_id=book_id, name=page.title, markdown=page.body)
        result.created.append(path)
        _emit(on_event, f"create {_rel(root, path)}")
        snap.after_create(new_id)
        page.page_id, page.book_id = new_id, book_id
        stamp(page, now=now)
        path.write_text(page_to_markdown(page), encoding="utf-8")


def _remote_markdown(client: BookStackClient, page_id: int) -> str:
    return (client.fetch_page(page_id).get("markdown") or "").strip()


def _book_id_for(books: dict[int, str], slug: str) -> int | None:
    for bid, bslug in books.items():
        if bslug == slug:
            return bid
    return None
