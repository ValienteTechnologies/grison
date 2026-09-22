""" "A folder mirrors a remote file set; an image line is a reference into it"
(BRIEF D1/D9) — the ONE mechanism, shared by :mod:`grison.adapters.gw_evidence`
(a report's ``evidence/`` folder <-> Ghostwriter evidence rows) and
:mod:`grison.adapters.bs_images` (a book's ``images/`` folder <-> BookStack's image
gallery). Not two implementations: both adapters hand this module a small
:class:`FileSetAdapter` and everything else — scanning, pairing, classification,
the change guard, the pre-write re-fetch guard, undo capture, bookkeeping — is one
code path.

Why this is not just another :class:`grison.engine.adapter.Adapter` run through
:mod:`grison.engine.documents`: that engine's :class:`~grison.engine.model.LocalDoc`
carries ``raw_text: str`` (decoded text) and :mod:`grison.engine.documents` always
writes a pulled/created record with :func:`grison.fsio.atomic_write_text` — both assume a
document. A file-set record's body is arbitrary bytes (an image is not always
UTF-8-decodable), so this module re-implements the same shape of loop (classify,
change guard, pre-write re-fetch, undo, bookkeeping) directly against bytes,
reusing every other primitive verbatim: :func:`grison.engine.classify.classify`,
:func:`grison.engine.identity.pair`, :func:`grison.engine.common.refetch_guard` (the
pre-write re-fetch guard itself — same drift-is-a-collision semantics, same event
wording, just handed this module's own bytes-aware refetch/hash functions instead
of an :class:`~grison.engine.adapter.Adapter`'s), :class:`grison.engine.state.
StateStore`, :class:`grison.engine.undo.Snapshot`/:class:`~grison.engine.undo.
UndoOp`, :class:`grison.index.Index`, and the same :class:`~grison.engine.model.
Outcome`/:class:`~grison.engine.model.Event`/:class:`~grison.engine.model.
KindSummary` types — so ``grison status``/``--json`` and ``grison undo`` treat a
file set exactly like any other engine-managed kind. This is the "seam" ENGINE.md's
package list calls out: "a record type whose body is bytes, not a document".

This package is that module, split into one file per concern (round 1 of the
filesets rework — pure moves, no behaviour change): :mod:`.captions` (the
caption-agreement rule), :mod:`.references` (LOCAL/REMOTE canonical prose and
the embed/cross-reference identity fold), :mod:`.model` (the shared dataclasses/
protocol and the small bytes/canonical-hash primitives), :mod:`.classify` (the
per-record classification that produces a :class:`~grison.engine.model.Plan`),
:mod:`.guards` (the pre-write re-fetch guard, the collision sidecar, undo
preimage/restore), :mod:`.apply_remote`/:mod:`.apply_local` (the outcome-specific
writes), :mod:`.dispatch` (the per-plan apply loop), and :mod:`.sync` (the
top-level :func:`sync_fileset` orchestration). Every name another module or test
imports from :mod:`grison.engine.filesets` is re-exported here unchanged.

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
    text (:func:`_substitute_ref_identity`, used by :func:`canonical_prose`;
    :func:`canonical_remote_prose` gets the same exclusion for free from
    ``html_to_md``'s own resolver contract), this rewrite can never make that
    document look locally edited: its content hash is unaffected by
    construction, not by remembering to exclude the rewrite from the change
    guard.

Cross-references (D1, restated): a plain link into ``evidence/<file>`` inline in a
sentence — ``[caption](evidence/file.png "desc")``, no ``!`` — names the same
remote identity an embed does but carries no caption/description of its own (the
link text is synthesised deterministically on the remote side, never preserved —
see :mod:`grison.markdown.converter`'s module docstring). :func:`_substitute_ref_
identity` folds BOTH an embed's and a cross-reference's destination into the same
identity token :func:`canonical_prose`/:func:`canonical_remote_prose` agree on —
one substitution for both forms (they differ only in the leading ``!``), by
POSITION from the one real parse (:mod:`grison.markdown.refscan`'s
:func:`~grison.markdown.refscan.scan_refs`/:func:`~grison.markdown.refscan.
ref_spans`, matched by ordinal index), never a second hand-rolled matching
regex — and ONLY for a reference :func:`_is_grison_reference` recognises: an
embed always; a cross-reference only when its destination starts with
``evidence/``, exactly mirroring the converter's own push-side rule, so an
ordinary external link is never touched on the local side either (the remote
side already never touches one, by construction — see
:func:`canonical_remote_prose`).
"""

from __future__ import annotations

from .captions import (
    CaptionConflict,
    CaptionTooLong,
    ReferenceCaption,
    collect_captions,
    rewrite_captions,
    strip_embed_captions,
)
from .guards import undo_restore
from .model import (
    FileSetAdapter,
    FileSetResult,
    RunOptions,
    caption_only_canonical,
)
from .references import (
    ResolvesEmbeds,
    canonical_prose,
    canonical_remote_prose,
)
from .sync import sync_fileset

__all__ = [
    "CaptionConflict",
    "CaptionTooLong",
    "FileSetAdapter",
    "FileSetResult",
    "ReferenceCaption",
    "ResolvesEmbeds",
    "RunOptions",
    "canonical_prose",
    "canonical_remote_prose",
    "caption_only_canonical",
    "collect_captions",
    "rewrite_captions",
    "strip_embed_captions",
    "sync_fileset",
    "undo_restore",
]
