"""``findings/library/*.md`` <-> Ghostwriter ``finding`` rows — see
:mod:`grison.adapters.gw_findings`'s own docstring for the shared shape."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from grison.engine.adapter import AdapterMode
from grison.engine.model import Canonical, LocalDoc, RemoteRecord, Veto
from grison.formats import finding as finding_fmt
from grison.formats.common import FormatError
from grison.model.enums import FindingType, Severity
from grison.remote.ghostwriter import GhostwriterClient

from .common import (
    _SECTIONS,
    _gw_fields,
    _plain_canonical,
    _remote_plain_canonical,
    _remote_sections_canonical,
    _sections_canonical,
    _split_tags,
    _tags_for,
    _title_of,
)


@dataclass(frozen=True)
class _LibraryDoc:
    doc: finding_fmt.FindingDoc


class GwLibraryFindingAdapter:
    """``findings/library/*.md`` <-> Ghostwriter ``finding`` rows. No evidence, no
    ``reportId`` — a library finding never has a real
    :class:`~grison.adapters._gw_common.IndexRefResolver` (see package docstring);
    ``ctx`` is a :class:`~grison.remote.ghostwriter.GhostwriterClient` directly
    (no report scoping needed)."""

    kind = "gw.finding"
    mode: AdapterMode = "read-write"

    def scan_local(self, root: Path) -> Iterable[LocalDoc]:
        base = root / "findings" / "library"
        if not base.is_dir():
            return
        for md in sorted(base.glob("*.md")):
            if md.name.endswith(".remote.md"):
                continue
            rel = PurePosixPath(md.relative_to(root).as_posix())
            raw = md.read_text(encoding="utf-8")
            try:
                doc = finding_fmt.parse(raw, path=Path(rel))
            except FormatError:
                yield LocalDoc(path=rel, doc=None, raw_text=raw)
                continue
            yield LocalDoc(path=rel, doc=_LibraryDoc(doc), raw_text=raw)

    def fetch_remote(self, ctx: GhostwriterClient) -> dict[int, RemoteRecord]:
        out = {}
        tag_map = ctx.fetch_tag_map()
        for row in ctx.fetch_findings():
            tags = tag_map.get(("finding", row["id"]), [])
            out[row["id"]] = RemoteRecord(id=row["id"], data={**row, "_tags": tags})
        return out

    def refetch(self, ctx: GhostwriterClient, id: int) -> RemoteRecord | None:
        row = ctx.finding_by_pk(id)
        if row is None:
            return None
        tags = ctx.fetch_tags_for("finding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def canonical_local(self, doc: _LibraryDoc | None) -> Canonical:
        if doc is None:
            return {"_invalid": True}
        return {**_plain_canonical(doc.doc), "sections": _sections_canonical(doc.doc, None)}

    def canonical_remote(self, data: dict[str, Any]) -> Canonical:
        tags = data.get("_tags", [])
        return {
            **_remote_plain_canonical(data, tags),
            "sections": _remote_sections_canonical(data, None),
        }

    def render_local(self, data: dict[str, Any], *, path: PurePosixPath) -> str:
        del path
        from grison.markdown.converter import html_to_md

        cwe, plain = _split_tags(data.get("_tags", []))
        doc = finding_fmt.FindingDoc(
            severity=Severity.from_gw_id(data["severityId"]),
            finding_type=FindingType.from_gw_id(data["findingTypeId"]),
            cvss=finding_fmt.FindingCvss(vector=data["cvssVector"])
            if data.get("cvssVector")
            else None,
            cwe=cwe,
            tags=plain,
            title=_title_of(data),
            # headings=True: see _remote_sections_canonical's comment above.
            **{f: html_to_md(data.get(f) or "", headings=True) for f in _SECTIONS},
        )
        return finding_fmt.dump(doc)

    def default_path(self, data: dict[str, Any], *, root: Path) -> PurePosixPath:
        del root
        from grison.sinks.file_sink import slugify

        return PurePosixPath("findings", "library", f"{slugify(_title_of(data))}.md")

    def relocated_path(self, data: dict[str, Any], *, current: PurePosixPath) -> PurePosixPath:
        del data
        return current

    def create(self, ctx: GhostwriterClient, doc: _LibraryDoc) -> RemoteRecord:
        fields = _gw_fields(doc.doc, None, instance=False)
        row = ctx.insert_finding(fields)
        ctx.set_tags(row["id"], "finding", _tags_for(doc.doc))
        tags = ctx.fetch_tags_for("finding", row["id"])
        return RemoteRecord(id=row["id"], data={**row, "_tags": tags})

    def update(self, ctx: GhostwriterClient, id: int, doc: _LibraryDoc) -> RemoteRecord:
        fields = _gw_fields(doc.doc, None, instance=False)
        row = ctx.update_finding(id, fields)
        ctx.set_tags(id, "finding", _tags_for(doc.doc))
        tags = ctx.fetch_tags_for("finding", id)
        return RemoteRecord(id=id, data={**row, "_tags": tags})

    def delete(self, ctx: GhostwriterClient, id: int) -> None:
        ctx.delete_finding(id)

    def restore(self, ctx: GhostwriterClient, preimage: Any) -> RemoteRecord:
        """Undo's inverse: put the pre-image back onto the SAME id when the row
        still exists (undoing a PUSH — the row is edited, not gone), else recreate
        it (undoing a DELETE_REMOTE — the id is gone for good; Ghostwriter has no
        restore-by-id). Same two-way shape as :meth:`~grison.adapters.gw_findings.
        reported.GwReportedFindingAdapter.restore`."""
        row = preimage
        fields = {k: v for k, v in row.items() if k not in ("id", "_tags")}
        tags = row.get("_tags", [])
        pid = row.get("id")
        if pid is not None and ctx.finding_by_pk(pid) is not None:
            updated = ctx.update_finding(pid, fields)
            ctx.set_tags(pid, "finding", tags)
            return RemoteRecord(id=pid, data={**updated, "_tags": tags})
        new = ctx.insert_finding(fields)
        if tags:
            ctx.set_tags(new["id"], "finding", tags)
        return RemoteRecord(id=new["id"], data={**new, "_tags": tags})

    def veto(self, local: Any | None, remote: Any | None) -> Veto | None:
        del local, remote
        return None

    def remote_label(self, data: Any) -> str:
        title = data.get("title") if isinstance(data, dict) else None
        rid = data.get("id") if isinstance(data, dict) else None
        return (
            f'"{title or "(untitled)"}" (finding {rid})'
            if rid is not None
            else f'"{title or "(untitled)"}"'
        )
