"""Ghostwriter evidence, on the shared file-set mechanism (BRIEF D1;
:mod:`grison.engine.filesets`). Ghostwriter >= 7.2.0 evidence rows are report-scoped
(``reportId``, required) — there is no ``findingId`` column at all, so grison mirrors
one report's rows into that report's own ``evidence/`` directory. One
:class:`GwEvidenceAdapter` instance is scoped to exactly one report; the findings
CLI phase constructs one per indexed ``gw.report`` directory and calls
:func:`grison.engine.filesets.sync_fileset` once per report, so classification/undo/
the change guard never mix rows across reports (a change guard withholding "the
evidence of report A" must never also count report B's rows).

``friendlyName`` (BRIEF D1/B): never authored, never relied on afterward. grison sets
it once at upload time to the file's stem, de-duplicated against every OTHER
friendlyName already used in the report (not just ones grison itself uploaded), and
never updates it again — :meth:`GwEvidenceAdapter.update_caption` only ever sends
``caption``/``description``. A ``friendlyName`` update fires a Ghostwriter-side
event trigger that rewrites ``{{.Name}}``/``{{ref .Name}}`` across every finding in
the report; grison must never trigger that from a routine caption edit.

Local filename (BRIEF B): on pull, the basename of the row's ``document`` column
(Ghostwriter's own stored path, which may differ from what was uploaded on a
collision — Django appends ``_<rand>``) — stable afterward, like every other
grison-created name (D3).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from grison.adapters._gw_common import GWContext
from grison.engine.filesets import caption_only_canonical
from grison.engine.model import RemoteRecord
from grison.errors import GrisonError
from grison.remote.ghostwriter.limits import (
    EVIDENCE_ALLOWED_EXTENSIONS,
    EVIDENCE_CAPTION_MAX_CHARS,
    evidence_extension,
    is_allowed_evidence_extension,
)


def _check_caption_length(caption: str) -> None:
    """Defense in depth, not the primary gate: ``grison validate`` (REF-010,
    :mod:`grison.validator.core.reports`) already refuses to push an over-long caption
    via the ordinary validation gate (ENGINE.md 'Apply loop' item 1) — a well-behaved
    sync never reaches this. This only fires for a caller that skipped validation (a
    hand-built preimage, a stale plan replayed by ``grison undo``, ...); it turns what
    would otherwise be Ghostwriter's own opaque rejection into a clear, local error
    naming the same limit :mod:`grison.remote.ghostwriter.limits` and the validator
    both check."""
    if len(caption) > EVIDENCE_CAPTION_MAX_CHARS:
        raise GrisonError(
            f"evidence caption is {len(caption)} characters, over Ghostwriter's "
            f"{EVIDENCE_CAPTION_MAX_CHARS}-character limit — grison validate (REF-010) "
            "should have refused this before push"
        )


def _check_uploadable(filename: str, caption: str) -> None:
    """Like :func:`_check_caption_length`, but also checks ``filename``'s extension
    (REF-009) — used by :meth:`GwEvidenceAdapter.upload`/``restore`` (which both send
    a fresh file), never by :meth:`GwEvidenceAdapter.update_caption` (which never
    touches the uploaded file itself)."""
    if not is_allowed_evidence_extension(filename):
        raise GrisonError(
            f"evidence file {filename!r} has extension {evidence_extension(filename)!r}, "
            f"not one of Ghostwriter's allowed evidence extensions "
            f"({', '.join(sorted(EVIDENCE_ALLOWED_EXTENSIONS))}) — grison validate (REF-009) "
            "should have refused this before push"
        )
    _check_caption_length(caption)


def _row_to_data(row: dict[str, Any]) -> dict[str, Any]:
    document = row.get("document") or ""
    filename = PurePosixPath(document).name if document else f"evidence-{row['id']}.bin"
    return {
        "filename": filename,
        "caption": row.get("caption") or "",
        "description": row.get("description") or "",
        "friendly_name": row.get("friendlyName") or "",
        "reportId": row.get("reportId"),
    }


@dataclass
class GwEvidenceAdapter:
    """One report's evidence file set — a
    :class:`grison.engine.filesets.FileSetAdapter`. ``ctx`` for every method below
    is a :class:`~grison.adapters._gw_common.GWContext`."""

    report_id: int
    kind: str = "gw.evidence"
    supports_caption: bool = True
    caption_max_chars: int | None = EVIDENCE_CAPTION_MAX_CHARS

    def list_remote(self, ctx: GWContext) -> dict[int, RemoteRecord]:
        rows = ctx.all_evidence()
        out: dict[int, RemoteRecord] = {}
        for row in rows:
            if row.get("reportId") != self.report_id:
                continue
            out[row["id"]] = RemoteRecord(id=row["id"], data=_row_to_data(row))
        return out

    def fetch_body(self, ctx: GWContext, id: int) -> bytes:
        _filename, data = ctx.client.download_evidence(id)
        return data

    def upload(
        self,
        ctx: GWContext,
        *,
        filename: str,
        body: bytes,
        caption: str,
        description: str,
    ) -> RemoteRecord:
        _check_uploadable(filename, caption)
        friendly = self._dedupe_friendly_name(ctx, PurePosixPath(filename).stem)
        eid = ctx.client.upload_evidence(
            report_id=self.report_id,
            filename=filename,
            caption=caption,
            friendly_name=friendly,
            file_base64=base64.b64encode(body).decode("ascii"),
            description=description,
        )
        row = ctx.client.evidence_by_pk(eid)
        assert row is not None
        ctx.evidence_cache_upsert(row)
        return RemoteRecord(id=eid, data=_row_to_data(row))

    def _dedupe_friendly_name(self, ctx: GWContext, stem: str) -> str:
        return self._dedupe_friendly_name_for(ctx, self.report_id, stem)

    @staticmethod
    def _dedupe_friendly_name_for(ctx: GWContext, report_id: int, stem: str) -> str:
        # ctx.all_evidence() fetches org-wide evidence at most once per sync run
        # (GWContext's own cache) — every upload in this run keeps that cache
        # updated (see upload()/restore()'s evidence_cache_upsert calls), so this
        # de-dup check never re-fetches, even across many uploads in one run.
        existing = {
            r.get("friendlyName") or ""
            for r in ctx.all_evidence()
            if r.get("reportId") == report_id
        }
        if stem not in existing:
            return stem
        n = 2
        while f"{stem}-{n}" in existing:
            n += 1
        return f"{stem}-{n}"

    def update_caption(
        self,
        ctx: GWContext,
        id: int,
        *,
        caption: str,
        description: str,
    ) -> RemoteRecord:
        # D1: friendlyName is NEVER part of this call's `_set` — see module docstring.
        # Extension is never re-checked here (unlike upload()/restore()): a caption
        # edit never touches the uploaded file itself, only REF-010's caption limit
        # applies.
        _check_caption_length(caption)
        ctx.client.update_evidence(id, {"caption": caption, "description": description})
        row = ctx.client.evidence_by_pk(id)
        assert row is not None
        ctx.evidence_cache_upsert(row)
        return RemoteRecord(id=id, data=_row_to_data(row))

    def delete(self, ctx: GWContext, id: int) -> None:
        ctx.client.delete_evidence(id)
        ctx.evidence_cache_remove(id)

    def refetch(self, ctx: GWContext, id: int) -> RemoteRecord | None:
        row = ctx.client.evidence_by_pk(id)
        if row is None:
            return None
        return RemoteRecord(id=id, data=_row_to_data(row))

    def restore(self, ctx: GWContext, preimage: dict[str, Any]) -> RemoteRecord:
        """Re-upload into the preimage's OWN report — never ``self.report_id``:
        ``grison.engine.undo.replay``'s adapter map holds one entry per bare kind
        string, so the instance replaying this op may be bound to a different
        report than the one this evidence actually came from (see
        :func:`grison.engine.filesets._preimage`'s docstring). A preimage with no
        ``reportId`` at all can only mean a corrupt or pre-D1 snapshot — grison never
        guesses a scope by falling back to ``self.report_id``, which would silently
        restore the row into whichever report this replay's adapter instance happens
        to be bound to."""
        report_id = preimage.get("reportId")
        if report_id is None:
            raise GrisonError(
                f"evidence snapshot for {preimage.get('filename', '(unknown file)')!r} has "
                "no reportId recorded — cannot determine which report to restore it into"
            )
        _check_uploadable(preimage["filename"], preimage.get("caption", ""))
        body = base64.b64decode(preimage["body_b64"])
        friendly = self._dedupe_friendly_name_for(
            ctx, report_id, PurePosixPath(preimage["filename"]).stem
        )
        eid = ctx.client.upload_evidence(
            report_id=report_id,
            filename=preimage["filename"],
            caption=preimage.get("caption", ""),
            friendly_name=friendly,
            file_base64=base64.b64encode(body).decode("ascii"),
            description=preimage.get("description", ""),
        )
        row = ctx.client.evidence_by_pk(eid)
        assert row is not None
        ctx.evidence_cache_upsert(row)
        return RemoteRecord(id=eid, data=_row_to_data(row))

    def remote_label(self, data: Any) -> str:
        name = data.get("filename") or "(unknown)" if isinstance(data, dict) else str(data)
        return f'"{name}" (evidence)'

    def canonical_remote(self, data: dict[str, Any]) -> dict[str, Any]:
        """Undo-only (:class:`grison.engine.adapter.PushUndoAdapter`): the
        caption-push drift check `grison.engine.undo` runs before restoring a
        pre-image over a record that may have been edited again since — see
        :func:`~grison.engine.filesets.caption_only_canonical`'s docstring for
        why this never needs bytes (D1: immutable per id) and is never called
        for a genuine ``sync_fileset`` classification, only for undo replay."""
        return caption_only_canonical(data)
