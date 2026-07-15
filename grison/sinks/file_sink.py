"""The file sink — write findings as markdown documents, idempotently.

Filenames are cosmetic (sync matches records by id, not name): ``slug(title).md``,
disambiguated by a dedupe key when two *different* findings share a slug. Re-writing
identical content is reported as *unchanged*, never duplicated.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from grison.markdown import finding_to_markdown
from grison.model import Finding


def slugify(text: str) -> str:
    """Filesystem-friendly slug: ascii-fold, lowercase, non-alnum → ``-``."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug or "finding"


@dataclass
class SinkResult:
    written: list[Path] = field(default_factory=list)  # newly created or changed
    unchanged: list[Path] = field(default_factory=list)  # identical content already there
    errors: list[str] = field(default_factory=list)


def _stems(findings: list[Finding], keys: list[str] | None) -> list[str]:
    """Assign a filename stem per finding, disambiguating slug collisions.

    Two passes: first break a slug collision with the dedupe key (or a content
    hash if there's no key). That alone isn't enough — two *different* findings
    can share both slug and key (e.g. the same scanner plugin firing in two
    export files parsed in one run) and land on the same stem again. The second
    pass catches any stem still shared by more than one finding and appends a
    content digest to every member of that group, so distinct findings never
    collapse onto the same path. Digests are order-independent, so re-runs stay
    idempotent; two findings with genuinely identical content still collapse to
    one stem, which is correct dedupe.
    """
    base = [slugify(f.title) for f in findings]
    counts: dict[str, int] = {}
    for b in base:
        counts[b] = counts.get(b, 0) + 1
    stems: list[str] = []
    for i, (f, b) in enumerate(zip(findings, base, strict=True)):
        if counts[b] == 1:
            stems.append(b)
            continue
        # collision → suffix with the dedupe key, or a content hash as a fallback
        if keys and keys[i]:
            stems.append(f"{b}-{slugify(keys[i])}")
        else:
            digest = hashlib.sha256(finding_to_markdown(f).encode()).hexdigest()[:8]
            stems.append(f"{b}-{digest}")

    # second pass: a stem can still collide (same slug + same dedupe key) —
    # break the tie with a content digest for every member of the surviving group
    stem_counts: dict[str, int] = {}
    for s in stems:
        stem_counts[s] = stem_counts.get(s, 0) + 1
    for i, (f, s) in enumerate(zip(findings, stems, strict=True)):
        if stem_counts[s] > 1:
            digest = hashlib.sha256(finding_to_markdown(f).encode()).hexdigest()[:8]
            stems[i] = f"{s}-{digest}"
    return stems


class FileSink:
    """Writes findings to ``out_dir`` as ``<stem>.md`` (implements the Sink port)."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def write(
        self,
        findings: list[Finding],
        *,
        keys: list[str] | None = None,
        dry_run: bool = False,
    ) -> SinkResult:
        result = SinkResult()
        stems = _stems(findings, keys)
        for f, stem in zip(findings, stems, strict=True):
            path = self.out_dir / f"{stem}.md"
            try:
                content = finding_to_markdown(f)
                if path.exists() and path.read_text(encoding="utf-8") == content:
                    result.unchanged.append(path)
                    continue
                if not dry_run:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                result.written.append(path)
            except Exception as e:  # noqa: BLE001 — isolate one finding, keep the batch
                result.errors.append(f"{path}: {e}")
        return result
