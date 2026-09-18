""""A folder mirrors a remote file set; an image line is a reference into it"
(BRIEF D1/D9) — the ONE mechanism, shared by :mod:`grison.adapters.gw_evidence`
(a report's ``evidence/`` folder <-> Ghostwriter evidence rows) and
:mod:`grison.adapters.bs_images` (a book's ``images/`` folder <-> BookStack's image
gallery). Not two implementations: both adapters hand this module a small
:class:`FileSetAdapter` and everything else — scanning, pairing, classification,
the change guard, the pre-write re-fetch guard, undo capture, bookkeeping — is one
code path.

Why this is not just another :class:`grison.engine.adapter.Adapter` run through
:mod:`grison.engine.apply`: that engine's :class:`~grison.engine.model.LocalDoc`
carries ``raw_text: str`` (decoded text) and ``apply.py`` always writes a pulled/
created record with :func:`grison.fsio.atomic_write_text` — both assume a
document. A file-set record's body is arbitrary bytes (an image is not always
UTF-8-decodable), so this module re-implements the same shape of loop (classify,
change guard, pre-write re-fetch, undo, bookkeeping) directly against bytes,
reusing every other primitive verbatim: :func:`grison.engine.classify.classify`,
:func:`grison.engine.identity.pair`, :func:`grison.engine.apply.refetch_guard` (the
pre-write re-fetch guard itself — same drift-is-a-collision semantics, same event
wording, just handed this module's own bytes-aware refetch/hash functions instead
of an :class:`~grison.engine.adapter.Adapter`'s), :class:`grison.engine.state.
StateStore`, :class:`grison.engine.undo.Snapshot`/:class:`~grison.engine.undo.
UndoOp`, :class:`grison.index.Index`, and the same :class:`~grison.engine.model.
Outcome`/:class:`~grison.engine.model.Event`/:class:`~grison.engine.model.
KindSummary` types — so ``grison status``/``--json`` and ``grison undo`` treat a
file set exactly like any other engine-managed kind. This is the "seam" ENGINE.md's
package list calls out: "a record type whose body is bytes, not a document".

Two independent rules (D1, restated generically):

  (a) the folder mirrors the remote file set whether or not anything references a
      file: a new local file uploads, a new remote row downloads, a locally
      deleted (indexed) file's remote row is deleted, and bytes changing under an
      unchanged name creates a NEW remote row (old row deleted, index repointed) —
      Ghostwriter/BookStack have no "replace bytes in place" operation, so this is
      modelled as CREATE+DELETE_REMOTE (:data:`Outcome.MOVE_EDIT` is reused as the
      "re-upload" outcome purely as an internal signal — never a real update
      call), and the caller is responsible for then re-pushing every document
      that referenced the old id, automatically, in the same run (D1) —
      :func:`canonical_prose` (LOCAL side, id resolved through the live index)
      paired with :func:`canonical_remote_prose` (REMOTE side, the LITERAL id
      already present in the remote's own stored HTML/text, never re-resolved
      through that same live index) is the mechanism that makes that re-push
      happen "by construction": a reupload leaves the referencing record's own
      remote content untouched, so its canonical form doesn't move off ``base``,
      while the LOCAL side's does (the id it resolves to changed) — the record
      classifies PUSH, never a silent CLEAN or an unresolved-reference PULL
      overwrite.
  (b) an image line (found via :mod:`grison.markdown.refscan`) in a referencing
      document is a reference; removing a reference never deletes the file.

Two files sharing a stem (REF-003) and two non-empty captions disagreeing
(REF-004) are hard validator failures BEFORE sync ever runs (`grison.validator`) —
this module's own :func:`collect_captions` re-derives the same agreement rule
defensively (never silently guesses when it sees a conflict the validator
should have already caught), but is not the primary enforcement. A conflict that
slips past that gate never aborts the sync: :func:`sync_fileset` degrades just
that one file to "no local caption opinion" (the same branch a file with no
non-empty alt anywhere already takes — see below) and reports it as a `failed`
event naming REF-004 and the disagreeing documents (ENGINE.md §5 per-record
isolation — one bad file's caption conflict must never take down every other
file, or the findings/wiki that reference them, in the same sync).

Caption/description sync — the exact rule an adapter that sets
``supports_caption=True`` (only :mod:`grison.adapters.gw_evidence`; BookStack's
gallery has no caption column at all, so :mod:`grison.adapters.bs_images` sets it
``False`` and captions never enter that adapter's classification at all) gets for
free:

  - every embed of a file whose alt is non-empty is the file's LOCAL caption
    opinion (all such alts are equal — REF-004); title text is the description.
  - a file with NO non-empty alt anywhere has no local opinion at all: this
    module then treats the file's caption as whatever the REMOTE currently says
    (never a push), so classification never invents a change on that axis.
  - a genuine local opinion that differs from the remote's current caption is a
    metadata PUSH (``update_caption``, never touching bytes/friendlyName).
  - a genuine remote caption change (no local opinion, or the local opinion
    still agrees with the OLD remote value) is a PULL: :func:`sync_fileset`
    returns ``resolved_captions``, and the caller rewrites the alt text in every
    referencing document to match (:func:`rewrite_captions`). Because a
    referencing document's own canonical payload never includes caption/title
    text (:func:`_substitute_embed_identity`, used by :func:`canonical_prose`/
    :func:`canonical_remote_prose`), this rewrite can never make that document
    look locally edited: its content hash is unaffected by construction, not by
    remembering to exclude the rewrite from the change guard.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from grison.engine.apply import refetch_guard, sidecar_path
from grison.engine.classify import classify
from grison.engine.identity import Missing, Unindexed, pair
from grison.engine.model import (
    Event,
    KindSummary,
    Outcome,
    Plan,
    RemoteRecord,
    VetoSeverity,
)
from grison.engine.state import StateStore
from grison.engine.undo import Snapshot, UndoOp
from grison.fsio import atomic_write_bytes
from grison.hashing import digest
from grison.index import Index, IndexKind
from grison.markdown.converter import ConverterError, html_to_md
from grison.markdown.refs import LocalRef, RemoteRef
from grison.markdown.refscan import scan_refs

#: Same values as grison.engine.apply's — one change-guard rule for every record
#: type (ENGINE.md), file sets included. A separate, literal copy rather than an
#: import of apply.py's private constants: this module implements its own loop
#: (see module docstring) and this is the one piece of apply.py it must not
#: silently drift from if that module's thresholds ever change.
_MASS_CHANGE_MIN = 5
_MASS_CHANGE_RATIO = 0.2

_REMOTE_WRITE_OUTCOMES = frozenset(
    {Outcome.PUSH, Outcome.CREATE, Outcome.DELETE_REMOTE, Outcome.MOVE_EDIT}
)
_LOCAL_WRITE_OUTCOMES = frozenset({Outcome.PULL, Outcome.DELETE_LOCAL})


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    force_local: frozenset[PurePosixPath] = frozenset()
    force_remote: frozenset[PurePosixPath] = frozenset()
    mass_change_ratio: float = _MASS_CHANGE_RATIO


@dataclass(frozen=True)
class ReferenceCaption:
    """One file's resolved local caption/description opinion, from every embed
    that references it across the documents :func:`collect_captions` was given
    (D1's caption rule, restated in the module docstring). ``has_opinion=False``
    means no reference had a non-empty alt anywhere — the file's caption then
    mirrors the remote's current value (never pushed)."""

    caption: str
    description: str
    has_opinion: bool


@dataclass(frozen=True)
class CaptionConflict:
    """One file whose referencing documents disagree on its caption (REF-004)
    slipping past the validator — :func:`collect_captions` reports it here
    instead of raising (module docstring): the file gets no entry in
    :func:`collect_captions`' other return value, which :func:`sync_fileset`'s
    callers already treat as "no local caption opinion" (mirrors the remote,
    never pushed), and :func:`sync_fileset` turns each conflict into its own
    ``failed`` event/:class:`~grison.engine.model.Plan` — one broken file, not
    the whole sync."""

    name: str
    captions: tuple[str, ...]
    docs: tuple[str, ...]  # one representative referencing document per caption

    @property
    def detail(self) -> str:
        return (
            f"conflicting captions {list(self.captions)!r} across "
            f"{', '.join(self.docs)} (REF-004)"
        )


def collect_captions(
    doc_bodies: dict[PurePosixPath, str], *, folder: PurePosixPath
) -> tuple[dict[str, ReferenceCaption], list[CaptionConflict]]:
    """Scan every document in ``doc_bodies`` for embeds whose path resolves inside
    ``folder`` (``<folder-name>/name`` or, one level down, ``../<folder-name>/name``
    — callers pass whichever spellings their own REF rule accepts), keyed by
    filename. Returns ``(captions, conflicts)``: a name whose referencing documents
    disagree on a non-empty caption is left OUT of ``captions`` and reported in
    ``conflicts`` instead — a disagreement the validator should already have
    rejected (defense in depth, not primary enforcement — see module docstring),
    never guessed at and never raised past the one file it concerns."""
    by_name: dict[str, list[tuple[PurePosixPath, str, str]]] = {}
    prefix = f"{folder.name}/"
    for doc_path, body in doc_bodies.items():
        for ref in scan_refs(body):
            if ref.kind != "embed":
                continue
            name = _match_folder(ref.path, folder_name=folder.name, prefix=prefix)
            if name is None:
                continue
            by_name.setdefault(name, []).append((doc_path, ref.caption, ref.title))

    out: dict[str, ReferenceCaption] = {}
    conflicts: list[CaptionConflict] = []
    for name, entries in by_name.items():
        captions = {c for _d, c, _t in entries if c}
        descriptions = {t for _d, _c, t in entries if t}
        if len(captions) > 1:
            doc_by_caption: dict[str, PurePosixPath] = {}
            for doc_path, c, _t in entries:
                if c and c not in doc_by_caption:
                    doc_by_caption[c] = doc_path
            conflicts.append(CaptionConflict(
                name=name, captions=tuple(sorted(captions)),
                docs=tuple(str(doc_by_caption[c]) for c in sorted(captions)),
            ))
            continue
        caption = next(iter(captions), "")
        description = next(iter(descriptions), "") if len(descriptions) <= 1 else ""
        out[name] = ReferenceCaption(
            caption=caption, description=description, has_opinion=bool(captions)
        )
    return out, conflicts


def _match_folder(ref_path: str, *, folder_name: str, prefix: str) -> str | None:
    if ref_path.startswith(prefix):
        return ref_path[len(prefix):]
    alt_prefix = f"../{folder_name}/"
    if ref_path.startswith(alt_prefix):
        return ref_path[len(alt_prefix):]
    return None


# --- referencing-document canonicalization helpers --------------------------

_EMBED_LINE_RE = re.compile(
    r'^(!\[)([^\]]*)(\]\()([^)\s]+)(?:\s+"([^"]*)")?(\)\s*)$', re.MULTILINE
)


def strip_embed_captions(md: str) -> str:
    """A referencing document's OWN canonical payload must never include a
    file-set embed's caption/title (they belong to the remote row, not the
    document — see the module docstring): otherwise rewriting an alt on pull
    would change the document's content hash and make it look locally edited.
    Blanks every embed's alt/title text, keeping the path (identity) intact.
    :func:`canonical_prose`/:func:`canonical_remote_prose` fold this same
    exclusion into their own single-pass substitution (:func:`_substitute_
    embed_identity`, which drops alt/title AND replaces the path) rather than
    calling this directly; kept as its own named, independently useful/
    testable transform (the caption-blanking half in isolation)."""
    return _EMBED_LINE_RE.sub(lambda m: f"{m.group(1)}{m.group(3)}{m.group(4)}{m.group(6)}", md)


class ResolvesEmbeds(Protocol):
    def to_remote_id(self, path: str) -> int | None: ...


def _substitute_embed_identity(md: str, token_for: Callable[[str], str]) -> str:
    """Replace every embed's ``(path "title")`` with ``(<token_for(path)>)`` —
    dropping alt/title (same reason :func:`strip_embed_captions` does: they
    belong to the remote row, not the document) AND the authored path itself,
    which :func:`canonical_prose`/:func:`canonical_remote_prose` both replace
    with a stable REMOTE IDENTITY token so the two are directly comparable
    (see their docstrings)."""
    return _EMBED_LINE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(3)}{token_for(m.group(4))}{m.group(6)}", md
    )


def canonical_prose(md: str, resolver: ResolvesEmbeds) -> dict[str, Any]:
    """Canonical payload for one LOCAL prose field/section that may embed
    file-set references: the caption-stripped text, with every embed's
    AUTHORED PATH replaced by its CURRENTLY-resolved remote id (looked up
    through the live index/evidence rows ``resolver`` wraps) — never left as
    the path itself, which stays the stable text ``evidence/x.png`` even
    when the id underneath it changes (D1: bytes changing under an
    unchanged name creates a NEW remote row). This is what makes a reupload
    change THIS document's hash "by construction": :func:`canonical_remote_prose`
    (this record's OWN counterpart, called on the REMOTE side) folds the
    LITERAL id already present in the remote's stored HTML/text instead —
    never re-resolved through the same live lookup — so after a reupload
    with nothing else pushed, remote's payload is unchanged (still the OLD
    literal id, matching base) while this one changes (the NEW id), and the
    record classifies PUSH, not a silent CLEAN or an unresolved-reference
    PULL overwrite (D1: "replacing an image's bytes must re-push every
    finding referencing it")."""
    return {"text": _substitute_embed_identity(
        md, lambda path: _id_token(resolver.to_remote_id(path))
    )}


def _id_token(eid: int | None) -> str:
    return str(eid) if eid is not None else "unresolved"


@dataclass(frozen=True)
class _LiteralEmbedResolver:
    """The ``RefResolver`` :func:`canonical_remote_prose` hands ``html_to_md`` —
    used ONLY to compute a REMOTE record's canonical form, never for a real
    PULL (which uses the adapter's own, ordinary index-backed resolver, so a
    genuinely-broken reference still renders as a human-visible unresolved
    placeholder in the FILE grison writes). Resolves every embed
    successfully, always: a native ``data-evidence-id="N"``/gallery-id
    reference becomes the LITERAL ``N`` itself (never re-derived through the
    current index/evidence rows the way a real PULL resolver would); the
    legacy ``{{.friendlyName}}`` dot-form (BRIEF D1) has no literal id of its
    own, so it resolves through ``name_to_id`` (a snapshot of THIS run's
    evidence rows — the same one the real resolver would consult) when
    possible, else falls back to the literal name itself. Either way this
    NEVER queries the workspace index, so a stale id (its evidence row has
    since been reuploaded under a new id) still canonicalizes identically to
    how it did before that reupload — see :func:`canonical_prose`'s
    docstring for why that is exactly the property this record's REMOTE
    canonical form needs."""

    name_to_id: Mapping[str, int] | Callable[[], Mapping[str, int]] = field(
        default_factory=dict
    )

    def _rows(self) -> Mapping[str, int]:
        return self.name_to_id() if callable(self.name_to_id) else self.name_to_id

    def to_local(self, remote: RemoteRef) -> LocalRef | None:
        if remote.id is not None:
            return LocalRef(path=str(remote.id))
        if remote.name is not None:
            eid = self._rows().get(remote.name)
            return LocalRef(path=str(eid) if eid is not None else f"name:{remote.name}")
        return None

    def to_remote(self, path: str) -> RemoteRef | None:  # pragma: no cover — html_to_md
        return None  # only ever resolves PUSH direction refs, never called here


def canonical_remote_prose(
    html: str, *, headings: bool = False,
    name_to_id: Mapping[str, int] | Callable[[], Mapping[str, int]] = {},  # noqa: B006
) -> dict[str, Any]:
    """Canonical payload for one REMOTE prose field/section — the record-type
    counterpart :func:`canonical_prose` computes for the LOCAL side. Converts
    ``html`` to markdown through :class:`_LiteralEmbedResolver` (never the
    adapter's own, index-backed resolver) so every embed's identity in the
    result is the LITERAL one already present in ``html``, not one re-derived
    through whatever the index currently says — see that resolver's own
    docstring for why this is the fix for D1's "replacing an image's bytes
    must re-push every finding referencing it, automatically, in the same
    run": this payload only ever changes when ``html`` itself changes, so a
    reupload that leaves this record's own stored HTML untouched leaves this
    payload UNCHANGED (still equal to ``base``), while :func:`canonical_prose`'s
    LOCAL payload (computed via the live index) does change — the PUSH the
    classification table reaches from `L != base, R == base`. Never raises —
    same fallback every ``html_to_md`` caller in this codebase uses for
    unconvertible input: the raw HTML, verbatim."""
    resolver = _LiteralEmbedResolver(name_to_id=name_to_id)
    try:
        md = html_to_md(html or "", headings=headings, refs=resolver)
    except ConverterError:
        md = html or ""
    return {"text": md}


def rewrite_captions(md: str, resolved: dict[str, tuple[str, str]], *, folder_name: str) -> str:
    """Rewrite every embed's alt/title to match ``resolved`` (filename ->
    (caption, description)) — the PULL-side counterpart of a remote caption
    change (module docstring). A no-op for a file not in ``resolved`` or whose
    alt/title already matches. Safe against :func:`strip_embed_captions`'s own
    hash: this only ever changes text that function already excludes."""
    prefix = f"{folder_name}/"
    alt_prefix = f"../{folder_name}/"

    def _sub(m: re.Match[str]) -> str:
        path = m.group(4)
        name = None
        if path.startswith(prefix):
            name = path[len(prefix):]
        elif path.startswith(alt_prefix):
            name = path[len(alt_prefix):]
        if name is None or name not in resolved:
            return m.group(0)
        caption, description = resolved[name]
        title = f' "{description}"' if description else ""
        return f"{m.group(1)}{caption}{m.group(3)}{path}{title}{m.group(6)}"

    return _EMBED_LINE_RE.sub(_sub, md)


# --- the file-set adapter protocol + sync loop ------------------------------


class FileSetAdapter(Protocol):
    """One remote file set's binding (an evidence report, or a wiki book's
    gallery) — the file-set counterpart of
    :class:`grison.engine.adapter.Adapter`. ``data`` on every
    :class:`~grison.engine.model.RemoteRecord` this protocol hands back/receives
    is ``{"filename": str, "caption": str, "description": str}`` (bytes are
    fetched separately via :meth:`fetch_body`, so a sync that touches nothing
    never has to download file content for a row it isn't comparing)."""

    kind: str
    supports_caption: bool

    def list_remote(self, ctx: Any) -> dict[int, RemoteRecord]: ...

    def fetch_body(self, ctx: Any, id: int) -> bytes: ...

    def upload(
        self, ctx: Any, *, filename: str, body: bytes, caption: str, description: str
    ) -> RemoteRecord: ...

    def update_caption(
        self, ctx: Any, id: int, *, caption: str, description: str
    ) -> RemoteRecord: ...

    def delete(self, ctx: Any, id: int) -> None: ...

    def refetch(self, ctx: Any, id: int) -> RemoteRecord | None: ...

    def restore(self, ctx: Any, preimage: dict[str, Any]) -> RemoteRecord:
        """Undo's inverse of a delete: re-upload ``preimage``'s base64-encoded
        body under its original filename/caption/description (implementations
        should just return :func:`undo_restore`'s result)."""
        ...

    def remote_label(self, data: Any) -> str: ...


@dataclass
class FileSetResult:
    plans: list[Plan]
    events: list[Event]
    summary: KindSummary
    #: filename -> resolved (caption, description) for every file that has one —
    #: what the caller feeds :func:`rewrite_captions` for every referencing
    #: document in scope. Empty when the adapter doesn't support captions.
    resolved_captions: dict[str, tuple[str, str]] = field(default_factory=dict)


def _hash_bytes(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _cached_body_hash(state: StateStore, kind: str, id: int) -> str | None:
    """The remote body's hash as of the last sync that touched this id, from
    ``witness["body_hash"]`` — trustworthy forever, not just until the next
    change: D1 guarantees a file-set record's bytes are immutable for the life
    of its id (replacing bytes always creates a NEW id — see
    ``_apply_reupload``), so once cached this never needs re-verifying by
    download. ``None`` when no cache exists yet (a record indexed before this
    cache existed, or state was lost) — callers fall back to one download to
    establish the baseline."""
    st = state.get(kind, id)
    if st is None:
        return None
    value = st.witness.get("body_hash")
    return value if isinstance(value, str) else None


def _is_sidecar_name(name: str) -> bool:
    """True for a name :func:`grison.engine.apply.sidecar_path` would produce from
    some OTHER name in this same folder (``shot.png`` -> ``shot.remote.png``, an
    extension-less ``README`` -> ``README.remote``). The old check here
    (``name.endswith(".remote")``) only ever matched the extension-less case — a
    real ``<name>.remote.<ext>`` collision sidecar (the only kind an evidence/image
    folder ever actually has, since every file in one has an extension) always ends
    in the ORIGINAL extension, never in ``.remote`` itself, so it silently passed
    straight through as an ordinary local file (ENGINE.md §8: sidecars are never
    documents/files the engine scans — ``grison.engine.apply``'s own document-side
    scanners get this right for free because a document sidecar is always literally
    ``.remote.md``, a single fixed extension; a file-set sidecar's extension varies
    with the file it shadows, so this module needs the general shape check)."""
    p = PurePosixPath(name)
    if p.suffix == ".remote":
        return True
    return PurePosixPath(p.stem).suffix == ".remote"


def _local_files(root: Path, folder: PurePosixPath) -> dict[str, bytes]:
    d = root / folder
    if not d.is_dir():
        return {}
    return {
        p.name: p.read_bytes()
        for p in sorted(d.iterdir())
        if p.is_file() and not p.name.startswith(".") and not _is_sidecar_name(p.name)
    }


def _canonical(
    *, body_hash: str, caption: str, description: str, supports_caption: bool
) -> dict[str, Any]:
    if not supports_caption:
        return {"hash": body_hash}
    return {"hash": body_hash, "caption": caption, "description": description}


def _event(
    verb: str, *, path: PurePosixPath | None, label: str | None = None, detail: str = "",
    dry_run: bool = False, severity: VetoSeverity | None = None,
) -> Event:
    return Event(verb=verb, path=str(path) if path is not None else None, label=label,
                detail=detail, dry_run=dry_run, severity=severity)


def sync_fileset(  # noqa: PLR0913
    root: Path,
    ctx: Any,
    adapter: FileSetAdapter,
    folder: PurePosixPath,
    *,
    index: Index,
    state: StateStore,
    snapshot: Snapshot,
    doc_bodies: dict[PurePosixPath, str] | None = None,
    options: RunOptions | None = None,
) -> FileSetResult:
    """Sync one file set (one report's ``evidence/``, or one book's ``images/``).

    ``doc_bodies`` (only meaningful when ``adapter.supports_caption``) is every
    document in this file set's scope, already read — used to derive each
    file's local caption opinion (:func:`collect_captions`); omit/empty for an
    adapter with no caption concept at all.
    """
    options = options or RunOptions()
    kind = adapter.kind
    events: list[Event] = []
    summary = KindSummary(kind=kind)

    local_files = _local_files(root, folder)
    remote_rows = adapter.list_remote(ctx)
    captions, caption_conflicts = (
        collect_captions(doc_bodies or {}, folder=folder)
        if adapter.supports_caption else ({}, [])
    )

    indexed = {
        PurePosixPath(p): rec.id
        for p, rec in index.records.items()
        if rec.kind.value == kind and PurePosixPath(p).parent == folder
    }
    indexed_ids = set(indexed.values())
    name_by_id = {v: k.name for k, v in indexed.items()}

    present_names = set(local_files) & set(name_by_id.values())
    missing_names = set(name_by_id.values()) - set(local_files)
    unindexed_names = set(local_files) - set(name_by_id.values())
    remote_unindexed = set(remote_rows) - indexed_ids

    plans, paired_missing_names, paired_unindexed_names = _pairing_plans(
        kind, folder, missing_names, unindexed_names, indexed, state, local_files,
    )
    # a caption conflict (REF-004) that slipped past the validator degrades just
    # this one file to "no local caption opinion" (it has no entry in `captions`
    # above) and becomes its own FAILED record here — never an exception that
    # would abort every other file/finding in this sync (module docstring,
    # ENGINE.md §5 per-record isolation).
    for conflict in caption_conflicts:
        plans.append(Plan(kind=kind, outcome=Outcome.FAILED, path=folder / conflict.name,
                          reason=conflict.detail))

    for name in sorted(present_names - paired_missing_names):
        path = folder / name
        rid = indexed[path]
        plans.append(_classify_one(
            adapter, ctx, path, rid, local_files[name], remote_rows, state, options, captions,
        ))
    for name in sorted(missing_names - paired_missing_names):
        path = folder / name
        rid = indexed[path]
        plans.append(_classify_missing(adapter, ctx, path, rid, remote_rows, state, options))
    for name in sorted(unindexed_names - paired_unindexed_names):
        plans.append(Plan(kind=kind, outcome=Outcome.CREATE, path=folder / name))
    # A running "already spoken for" set, not just the pre-sync disk/index snapshot
    # (`local_files`/`indexed`): two remote rows named identically in the SAME sync
    # (e.g. two Ghostwriter evidence uploads both called "shot.png") would otherwise
    # both call `_dedupe_path` against the same unchanged pre-sync state and both
    # land on "shot.png", the second silently clobbering the first on disk and in
    # the index with no event at all. Seeded from local_files/indexed exactly like
    # before, then extended after every dedupe so each subsequent PULL_NEW in this
    # same loop sees the names already claimed by its predecessors.
    claimed_names = set(local_files) | {p.name for p in indexed}
    for rid in sorted(remote_unindexed):
        row = remote_rows[rid]
        path = _dedupe_path(folder / row.data["filename"], claimed_names)
        claimed_names.add(path.name)
        plans.append(Plan(kind=kind, outcome=Outcome.PULL_NEW, id=rid, remote=row, path=path))

    _apply_change_guard(plans, options)

    for p in plans:
        _apply_one(root, ctx, adapter, p, index, state, snapshot, events, options, local_files,
                  captions)
        summary.bump(p.outcome)
        if p.is_problem:
            label = str(p.path) if p.path is not None else adapter.remote_label(
                p.remote.data if p.remote is not None else {}
            )
            summary.problem_paths.append(label)

    _clear_stale_sidecars(root, plans)

    resolved: dict[str, tuple[str, str]] = {}
    if adapter.supports_caption:
        rows_after = _rows_after(remote_rows, plans)
        for name, data in rows_after.items():
            opinion = captions.get(name)
            if opinion is not None and opinion.has_opinion:
                resolved[name] = (opinion.caption, opinion.description)
            else:
                resolved[name] = (data.get("caption", ""), data.get("description", ""))

    return FileSetResult(plans=plans, events=events, summary=summary, resolved_captions=resolved)


def _dedupe_path(path: PurePosixPath, claimed_names: set[str]) -> PurePosixPath:
    """``claimed_names`` is every filename already spoken for IN THIS FOLDER — disk,
    index, or a sibling PULL_NEW plan already assigned earlier in the same loop (see
    the running ``claimed_names`` set built in :func:`sync_fileset`, which is what
    makes two same-named remote rows in one sync dedupe against EACH OTHER, not just
    against pre-sync state)."""
    if path.name not in claimed_names:
        return path
    stem, suffix = PurePosixPath(path.name).stem, PurePosixPath(path.name).suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if candidate.name not in claimed_names:
            return candidate
        n += 1


def _rows_after(
    remote_rows: dict[int, RemoteRecord], plans: list[Plan]
) -> dict[str, dict[str, Any]]:
    """Filename -> latest known {caption, description, ...} after this run's
    writes — what every referencing document's alt should read afterwards."""
    out: dict[str, dict[str, Any]] = {
        row.data["filename"]: row.data for row in remote_rows.values()
    }
    for p in plans:
        if p.outcome is Outcome.DELETE_REMOTE and p.remote is not None:
            out.pop(p.remote.data["filename"], None)
        elif p.remote is not None:
            out[p.remote.data["filename"]] = p.remote.data
    return out


def _pairing_plans(
    kind: str, folder: PurePosixPath, missing_names: set[str], unindexed_names: set[str],
    indexed: dict[PurePosixPath, int], state: StateStore, local_files: dict[str, bytes],
) -> tuple[list[Plan], set[str], set[str]]:
    """Move pairing: identical bytes only (D1 — changed bytes under the same
    name is a new row, never a move+edit — see module docstring). Both sides of
    the "identical" comparison are RAW BYTES hashes (``_hash_bytes``, the same
    hash space ``_cached_body_hash``/``Unindexed.content_hash`` use) — comparing
    a bytes hash against the canonical (body+caption+description) digest used
    elsewhere for ``state.base`` would never match even for byte-identical
    content (different hash spaces, so "identical" could never fire), which is
    why renaming a file used to always fall through to CREATE+DELETE_REMOTE
    instead of pairing as a MOVE."""
    missing: list[Missing] = []
    for name in missing_names:
        path = folder / name
        rid = indexed[path]
        missing.append(Missing(path=path, id=rid, base_hash=_cached_body_hash(state, kind, rid),
                               remote_content=None))
    unindexed: list[Unindexed] = []
    for name in unindexed_names:
        unindexed.append(Unindexed(path=folder / name, content="",
                                   content_hash=_hash_bytes(local_files[name])))
    result = pair(missing, unindexed)
    plans = [
        Plan(kind=kind, outcome=Outcome.MOVE, path=d.new_path, id=d.id, move_from=d.old_path)
        for d in result.decisions
    ]
    paired_missing = {d.old_path.name for d in result.decisions}
    paired_unindexed = {d.new_path.name for d in result.decisions}
    return plans, paired_missing, paired_unindexed


def _wrap_remote(row: RemoteRecord | None, canon_hash: str | None) -> RemoteRecord | None:
    """Carry classification's own canonical hash on the ``Plan.remote`` it hands to
    ``_apply_*`` — ``cached_hash`` is exactly the field
    :func:`grison.engine.apply.refetch_guard` (via this module's own ``_refetch_guard``
    below) compares a fresh re-fetch against, the same idiom
    :func:`grison.engine.apply._remote_hash` uses for a document adapter's
    skip-detail-fetch placeholder."""
    if row is None:
        return None
    return RemoteRecord(id=row.id, data=row.data, witness=row.witness, cached_hash=canon_hash,
                        losses=row.losses)


def _classify_one(  # noqa: PLR0913
    adapter: FileSetAdapter, ctx: Any, path: PurePosixPath, rid: int, body: bytes,
    remote_rows: dict[int, RemoteRecord], state: StateStore, options: RunOptions,
    captions: dict[str, ReferenceCaption],
) -> Plan:
    kind = adapter.kind
    row = remote_rows.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    local_hash = _hash_bytes(body)

    remote_body_hash: str | None = None
    remote_canon_hash: str | None = None
    if row is not None:
        # D1: bytes are immutable for a given id, so a cached digest never goes
        # stale — a clean sync (the common case) never downloads this file's
        # body at all. A record with no cache yet pays for one download, which
        # immediately reseeds state (CLASSIFY, not just apply, since a CLEAN
        # outcome never reaches _dispatch's own state.put) so every later sync
        # is warm too, not just the ones that happen to write something.
        cached = st.witness.get("body_hash") if st else None
        remote_body_hash = cached if isinstance(cached, str) else None
        if remote_body_hash is None:
            remote_body_hash = _hash_bytes(adapter.fetch_body(ctx, rid))
            state.put(kind, rid, base=base_hash,
                     witness={**(st.witness if st else {}), "body_hash": remote_body_hash})
        remote_canon_hash = digest(_canonical(
            body_hash=remote_body_hash, caption=row.data.get("caption", ""),
            description=row.data.get("description", ""),
            supports_caption=adapter.supports_caption,
        ))

    opinion = captions.get(path.name)
    if opinion is not None and opinion.has_opinion:
        local_caption, local_description = opinion.caption, opinion.description
    else:
        local_caption = row.data.get("caption", "") if row else ""
        local_description = row.data.get("description", "") if row else ""
    local_canon_hash = digest(_canonical(
        body_hash=local_hash, caption=local_caption, description=local_description,
        supports_caption=adapter.supports_caption,
    ))

    outcome = classify(
        indexed=True, local_present=True, remote_present=row is not None,
        local_hash=local_canon_hash, remote_hash=remote_canon_hash, base_hash=base_hash,
        force_local=path in options.force_local, force_remote=path in options.force_remote,
    )
    remote = _wrap_remote(row, remote_canon_hash)
    if outcome is Outcome.PUSH and remote_body_hash is not None and remote_body_hash != local_hash:
        # bytes changed under the same name — never a metadata-only update
        # (D1): re-upload as a new row instead (see _apply_reupload).
        return Plan(kind=kind, outcome=Outcome.MOVE_EDIT, path=path, id=rid, remote=remote,
                   base_hash=base_hash, reason="bytes changed under the same name")
    return Plan(kind=kind, outcome=outcome, path=path, id=rid, remote=remote, base_hash=base_hash)


def _classify_missing(
    adapter: FileSetAdapter, ctx: Any, path: PurePosixPath, rid: int,
    remote_rows: dict[int, RemoteRecord], state: StateStore, options: RunOptions,
) -> Plan:
    kind = adapter.kind
    row = remote_rows.get(rid)
    st = state.get(kind, rid)
    base_hash = st.base if st else None
    remote_hash = None
    if row is not None:
        cached = st.witness.get("body_hash") if st else None
        body_hash = cached if isinstance(cached, str) else _hash_bytes(adapter.fetch_body(ctx, rid))
        remote_hash = digest(_canonical(
            body_hash=body_hash,
            caption=row.data.get("caption", ""), description=row.data.get("description", ""),
            supports_caption=adapter.supports_caption,
        ))
    outcome = classify(
        indexed=True, local_present=False, remote_present=row is not None,
        local_hash=None, remote_hash=remote_hash, base_hash=base_hash,
        force_local=path in options.force_local, force_remote=path in options.force_remote,
    )
    return Plan(kind=kind, outcome=outcome, path=path, id=rid,
               remote=_wrap_remote(row, remote_hash), base_hash=base_hash)


def _apply_change_guard(plans: list[Plan], options: RunOptions) -> None:
    total = max(len(plans), 1)

    def _weight(p: Plan) -> int:
        return 2 if p.outcome in (Outcome.DELETE_REMOTE, Outcome.DELETE_LOCAL) else 1

    def _forced(p: Plan) -> bool:
        return p.path is not None and (p.path in options.force_local
                                       or p.path in options.force_remote)

    remote_writes = [p for p in plans if p.outcome in _REMOTE_WRITE_OUTCOMES and not _forced(p)]
    w = sum(_weight(p) for p in remote_writes)
    if w > _MASS_CHANGE_MIN and w > options.mass_change_ratio * total:
        for p in remote_writes:
            p.outcome = Outcome.WITHHELD

    local_writes = [p for p in plans if p.outcome in _LOCAL_WRITE_OUTCOMES and not _forced(p)]
    d = sum(_weight(p) for p in local_writes)
    if d > _MASS_CHANGE_MIN and d > options.mass_change_ratio * total:
        for p in local_writes:
            p.outcome = Outcome.WITHHELD


def _refetch_guard(
    ctx: Any, adapter: FileSetAdapter, p: Plan, state: StateStore, options: RunOptions,
) -> tuple[RemoteRecord | None, bool]:
    """The file-set binding of :func:`grison.engine.apply.refetch_guard` — same
    pre-write drift check every remote-destructive write (re-upload, caption push,
    delete-remote) must run immediately before its write, not just the raw
    ``fresh is None`` check these used to do. Bytes can never silently change
    under an id (D1: a body change always mints a new id — see
    ``_apply_reupload``), so the fresh comparison only needs the row's current
    caption/description; the body-hash half of the canonical digest is the one
    already cached from classification (:func:`_cached_body_hash`), reused as-is
    rather than re-downloaded."""
    if p.id is None:
        return None, False
    rid = p.id
    body_hash = _cached_body_hash(state, adapter.kind, rid) or ""
    forced = p.path is not None and p.path in options.force_local
    return refetch_guard(
        refetch=lambda: adapter.refetch(ctx, rid),
        expected_hash=p.remote.cached_hash if p.remote is not None else None,
        canonical_hash=lambda fresh: digest(_canonical(
            body_hash=body_hash, caption=fresh.data.get("caption", ""),
            description=fresh.data.get("description", ""),
            supports_caption=adapter.supports_caption,
        )),
        forced=forced,
    )


def _apply_one(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions, local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    try:
        _dispatch(root, ctx, adapter, p, index, state, snapshot, events, options, local_files,
                 captions)
    except Exception as e:  # noqa: BLE001 — per-record isolation (ENGINE.md §5)
        p.outcome = Outcome.FAILED
        p.reason = f"{type(e).__name__}: {e}"
        events.append(_event("failed", path=p.path,
                             label=adapter.remote_label(p.remote.data) if p.remote else None,
                             detail=p.reason))


def _dispatch(  # noqa: PLR0911, PLR0912, PLR0913
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions, local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    kind = adapter.kind
    dry = options.dry_run

    if p.outcome is Outcome.CLEAN:
        return
    if p.outcome is Outcome.FAILED:
        # a pre-built failure (today: a caption conflict `collect_captions`
        # degraded rather than raised — see `sync_fileset`) that never went
        # through classification/apply at all; just surface it.
        events.append(_event("failed", path=p.path, detail=p.reason))
        return
    if p.outcome is Outcome.REPAIR:
        # L == R already (that's what REPAIR means) — restamp base to the
        # CANONICAL hash both sides already agree on (body+caption+description,
        # same shape every other base in this module uses), not a bare body
        # hash: a mismatched format here would never equal either side's
        # canonical hash again, so every future sync would see base != L and
        # re-REPAIR forever instead of the "no-op from here on" REPAIR promises.
        if not dry and p.id is not None and p.path is not None and p.remote is not None:
            body = local_files.get(p.path.name)
            if body is not None:
                body_hash = _hash_bytes(body)
                st = state.get(kind, p.id)
                state.put(kind, p.id, base=digest(_canonical(
                    body_hash=body_hash, caption=p.remote.data.get("caption", ""),
                    description=p.remote.data.get("description", ""),
                    supports_caption=adapter.supports_caption,
                )), witness={**(st.witness if st else {}), "body_hash": body_hash})
        events.append(_event("repair", path=p.path))
        return
    if p.outcome is Outcome.WITHHELD:
        events.append(_event("withheld", path=p.path))
        return
    if p.outcome is Outcome.MOVE:
        assert p.move_from is not None
        if dry:
            events.append(_event("move", path=p.path, detail=f"from {p.move_from}", dry_run=True))
            return
        index.move(str(p.move_from), str(p.path))
        events.append(_event("move", path=p.path, detail=f"from {p.move_from}"))
        return
    if p.outcome is Outcome.FORGET:
        if not dry and p.path is not None and p.id is not None:
            index.remove(str(p.path))
            state.forget(kind, p.id)
        events.append(_event("forget", path=p.path, dry_run=dry))
        return
    if p.outcome is Outcome.DELETE_LOCAL:
        _apply_delete_local(root, p, index, state, events, dry)
        return
    if p.outcome is Outcome.CREATE:
        _apply_create(ctx, adapter, p, index, state, snapshot, events, dry, local_files, captions)
        return
    if p.outcome is Outcome.MOVE_EDIT:
        _apply_reupload(root, ctx, adapter, p, index, state, snapshot, events, options,
                        local_files, captions)
        return
    if p.outcome is Outcome.PUSH:
        _apply_caption_push(root, ctx, adapter, p, state, snapshot, events, options, captions)
        return
    if p.outcome in (Outcome.PULL, Outcome.PULL_NEW):
        _apply_pull(root, ctx, adapter, p, index, state, events, dry)
        return
    if p.outcome is Outcome.DELETE_REMOTE:
        _apply_delete_remote(root, ctx, adapter, p, index, state, snapshot, events, options)
        return
    if p.outcome is Outcome.COLLISION:
        # ENGINE.md §8: a COLLISION writes the remote version next to the file —
        # gated on `dry` (mirrors `grison.engine.apply._apply_collision`, the
        # equivalent classify-time-collision branch for documents): dry run
        # reports the same event but performs no write of any kind (ENGINE.md §9).
        if not dry:
            _write_collision_sidecar(root, ctx, adapter, p.path, p.remote)
        events.append(_event("collision", path=p.path, dry_run=dry))
        return


def _write_collision_sidecar(
    root: Path, ctx: Any, adapter: FileSetAdapter, path: PurePosixPath | None,
    remote: RemoteRecord | None,
) -> None:
    """Mirrors :func:`grison.engine.apply._write_collision_sidecar` for a file
    set's bytes (ENGINE.md §8): the remote version, fetched via
    :meth:`FileSetAdapter.fetch_body` and written atomically next to the file at
    :func:`~grison.engine.apply.sidecar_path` (``shot.png`` ->
    ``shot.remote.png``) — never a document: the scaffolded ``.gitignore``'s
    ``*.remote.*`` entry already covers it (unlike the document engine, a
    file-set sidecar's extension is whatever the shadowed file's is, not a fixed
    ``.md``, so a literal ``.remote.md`` pattern would have missed it — see
    :func:`_is_sidecar_name`). A no-op when there's no local path to sidecar next
    to, or no remote record to read bytes from (e.g. "edited locally, deleted
    remotely" — there is nothing on the server left to show)."""
    if path is None or remote is None:
        return
    body = adapter.fetch_body(ctx, remote.id)
    atomic_write_bytes(root / sidecar_path(path), body)


def _clear_stale_sidecars(root: Path, plans: list[Plan]) -> None:
    """Mirrors :func:`grison.engine.apply._clear_stale_sidecars` (ENGINE.md §8): a
    sidecar is cleared as soon as its record is no longer in collision (resolved
    by a force flag, or the two sides converged)."""
    for p in plans:
        if p.path is None or p.outcome is Outcome.COLLISION:
            continue
        (root / sidecar_path(p.path)).unlink(missing_ok=True)


def _apply_delete_local(
    root: Path, p: Plan, index: Index, state: StateStore, events: list[Event], dry: bool,
) -> None:
    """"deleted remotely" (D1: the folder mirrors the remote set both ways — a row
    that disappeared on the server removes the local mirror file too), reachable
    when a file's remote row is gone but the local copy still matches the last
    synced base (classify.py's ordinary DELETE_LOCAL row)."""
    assert p.path is not None and p.id is not None
    if dry:
        events.append(_event("delete-local", path=p.path, dry_run=True))
        return
    (root / p.path).unlink(missing_ok=True)
    index.remove(str(p.path))
    state.forget(p.kind, p.id)
    events.append(_event("delete-local", path=p.path))


def _apply_create(  # noqa: PLR0913
    ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], dry: bool, local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    assert p.path is not None
    if dry:
        events.append(_event("create", path=p.path, dry_run=True))
        return
    body = local_files[p.path.name]
    opinion = captions.get(p.path.name)
    caption = opinion.caption if opinion is not None and opinion.has_opinion else ""
    description = opinion.description if opinion is not None and opinion.has_opinion else ""
    row = adapter.upload(ctx, filename=p.path.name, body=body, caption=caption,
                         description=description)
    index.set(str(p.path), IndexKind(adapter.kind), row.id)
    snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=row.id))
    body_hash = _hash_bytes(body)
    state.put(adapter.kind, row.id, base=digest(_canonical(
        body_hash=body_hash, caption=row.data.get("caption", ""),
        description=row.data.get("description", ""), supports_caption=adapter.supports_caption,
    )), witness={**row.witness, "body_hash": body_hash})
    p.remote = row  # so _rows_after()'s caption-rewrite pass sees this newly-created row too
    events.append(_event("create", path=p.path))


def _preimage(adapter: FileSetAdapter, ctx: Any, row: RemoteRecord) -> dict[str, Any]:
    """The full ``row.data`` (adapter-specific — e.g. ``gw.evidence``'s ``reportId``,
    needed so ``restore()`` re-uploads into the SAME scope regardless of which
    scope-bound adapter instance ``grison undo`` happens to be replaying through:
    ``grison.engine.undo.replay``'s adapter map is keyed by bare kind string, one
    entry for every report/book, so a fileset adapter's ``restore`` must recover its
    own scope from the preimage, never from ``self``) plus the base64 body (D7:
    "deletes keep the bytes in the snapshot")."""
    body = adapter.fetch_body(ctx, row.id)
    return {**row.data, "body_b64": base64.b64encode(body).decode("ascii")}


def _apply_reupload(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions, local_files: dict[str, bytes],
    captions: dict[str, ReferenceCaption],
) -> None:
    """Bytes changed under the same name (D1): a new remote row, the old one
    deleted, the index repointed at the new id — never an in-place update. If a
    local caption opinion also changed in this same sync, it rides along on the
    re-upload (a fresh row's caption is set once, at create time — see
    ``upload``'s contract — there is no separate metadata PUSH to follow up
    with, since the plan that would have carried it was replaced by this one).

    Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a caption/description change (or
    the row vanishing outright) on the server since classification becomes a
    COLLISION — nothing uploaded, nothing deleted — instead of silently
    re-uploading over a change grison never saw."""
    assert p.path is not None and p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted:
        p.outcome = Outcome.COLLISION
        if not dry:
            _write_collision_sidecar(root, ctx, adapter, p.path, fresh)
        events.append(_event("collision", path=p.path,
                             detail="changed on the server since classification"))
        return
    if dry:
        events.append(_event("push", path=p.path, detail="bytes changed — new remote row",
                             dry_run=True))
        return
    body = local_files[p.path.name]
    opinion = captions.get(p.path.name)
    if opinion is not None and opinion.has_opinion:
        caption, description = opinion.caption, opinion.description
    else:
        caption = fresh.data.get("caption", "") if fresh else ""
        description = fresh.data.get("description", "") if fresh else ""
    new_row = adapter.upload(ctx, filename=p.path.name, body=body, caption=caption,
                             description=description)
    old_id = p.id
    if fresh is not None:
        snapshot.record(UndoOp(kind=adapter.kind, outcome="delete_remote", path=str(p.path),
                               id=old_id, remote_preimage=_preimage(adapter, ctx, fresh)))
        adapter.delete(ctx, old_id)
    snapshot.record(UndoOp(kind=adapter.kind, outcome="create", path=str(p.path), id=new_row.id))
    index.set(str(p.path), IndexKind(adapter.kind), new_row.id)
    state.forget(adapter.kind, old_id)
    body_hash = _hash_bytes(body)
    state.put(adapter.kind, new_row.id, base=digest(_canonical(
        body_hash=body_hash, caption=caption, description=description,
        supports_caption=adapter.supports_caption,
    )), witness={**new_row.witness, "body_hash": body_hash})
    p.remote = new_row  # so _rows_after()/the caller's caption-rewrite pass sees the new row
    events.append(_event("push", path=p.path, detail="bytes changed — new remote row"))


def _apply_caption_push(  # noqa: PLR0913
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
    captions: dict[str, ReferenceCaption],
) -> None:
    """Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a caption/description that changed
    on the server since classification (or the row being gone outright) is a
    COLLISION with the same wording the document engine uses, not a
    push-specific "deleted on the server" message — never a metadata write over a
    change grison never saw."""
    assert p.id is not None and p.path is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted or fresh is None:
        # fresh is None only reachable here when force-local overrode drifted for a
        # row that's now gone entirely (refetch_guard's own "not forced" rule) — a
        # caption push has no "recreate" fallback the way a reupload/delete-remote
        # does (there is nothing to attach the caption to), so it collides too.
        p.outcome = Outcome.COLLISION
        if not dry:
            _write_collision_sidecar(root, ctx, adapter, p.path, fresh)
        events.append(_event("collision", path=p.path,
                             detail="changed on the server since classification"))
        return
    if dry:
        events.append(_event("push", path=p.path, detail="caption/description", dry_run=True))
        return
    # Re-derive the winning local opinion exactly as _classify_one did (a local
    # opinion with no non-empty alt anywhere defers to the remote's own value,
    # which can never itself be the reason for a PUSH — see that function).
    opinion = captions.get(p.path.name)
    assert opinion is not None and opinion.has_opinion, (
        "a caption PUSH with no local opinion would mean local mirrored remote "
        "and could never have differed from base — classify() would not have "
        "produced PUSH for this record"
    )
    local_caption, local_description = opinion.caption, opinion.description
    snapshot.record(UndoOp(kind=adapter.kind, outcome="push", path=str(p.path), id=p.id,
                           remote_preimage=_preimage(adapter, ctx, fresh)))
    updated = adapter.update_caption(
        ctx, p.id, caption=local_caption, description=local_description
    )
    # A caption/description push never touches bytes (D1) — reuse the cached
    # digest instead of re-downloading; fall back to a download only if this
    # id somehow has no cached digest yet.
    cached = state.get(adapter.kind, p.id)
    cached_hash = cached.witness.get("body_hash") if cached else None
    body_hash = cached_hash if isinstance(cached_hash, str) else _hash_bytes(
        adapter.fetch_body(ctx, p.id)
    )
    state.put(adapter.kind, p.id, base=digest(_canonical(
        body_hash=body_hash, caption=updated.data.get("caption", ""),
        description=updated.data.get("description", ""), supports_caption=True,
    )), witness={**updated.witness, "body_hash": body_hash})
    p.remote = updated
    events.append(_event("push", path=p.path, detail="caption/description"))


def _apply_pull(
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    events: list[Event], dry: bool,
) -> None:
    assert p.remote is not None and p.path is not None
    row = p.remote
    path = p.path
    label = adapter.remote_label(row.data)
    is_new = index.get(str(path)) is None
    if dry:
        events.append(_event("pull", path=path, label=label if is_new else None, dry_run=True))
        return
    body = adapter.fetch_body(ctx, row.id)
    atomic_write_bytes(root / path, body)
    index.set(str(path), IndexKind(adapter.kind), row.id)
    body_hash = _hash_bytes(body)
    state.put(adapter.kind, row.id, base=digest(_canonical(
        body_hash=body_hash, caption=row.data.get("caption", ""),
        description=row.data.get("description", ""), supports_caption=adapter.supports_caption,
    )), witness={**row.witness, "body_hash": body_hash})
    events.append(_event("pull", path=path, label=label if is_new else None))


def _apply_delete_remote(
    root: Path, ctx: Any, adapter: FileSetAdapter, p: Plan, index: Index, state: StateStore,
    snapshot: Snapshot, events: list[Event], options: RunOptions,
) -> None:
    """Guarded by the SAME pre-write re-fetch check every remote-destructive write
    gets (:func:`_refetch_guard`, ENGINE.md §3): a row that changed on the server
    since classification is a COLLISION — nothing deleted — with the same wording
    the document engine uses, not a raw ``fresh is None`` check."""
    assert p.id is not None
    dry = options.dry_run
    fresh, drifted = _refetch_guard(ctx, adapter, p, state, options)
    if drifted:
        p.outcome = Outcome.COLLISION
        if not dry:
            _write_collision_sidecar(root, ctx, adapter, p.path, fresh)
        events.append(_event("collision", path=p.path,
                             detail="changed on the server since classification"))
        return
    if dry:
        events.append(_event("delete-remote", path=p.path, dry_run=True))
        return
    if fresh is not None:
        snapshot.record(UndoOp(kind=adapter.kind, outcome="delete_remote", path=str(p.path),
                               id=p.id, remote_preimage=_preimage(adapter, ctx, fresh)))
        adapter.delete(ctx, p.id)
    if p.path is not None:
        index.remove(str(p.path))
    state.forget(adapter.kind, p.id)
    events.append(_event("delete-remote", path=p.path))


def undo_restore(adapter: FileSetAdapter, ctx: Any, preimage: dict[str, Any]) -> RemoteRecord:
    """The shared body every file-set adapter's own ``restore`` delegates to —
    decode the snapshot's base64 body and re-upload it (D1: "undo (deletes keep
    the bytes in the snapshot)")."""
    body = base64.b64decode(preimage["body_b64"])
    return adapter.upload(
        ctx, filename=preimage["filename"], body=body, caption=preimage.get("caption", ""),
        description=preimage.get("description", ""),
    )
