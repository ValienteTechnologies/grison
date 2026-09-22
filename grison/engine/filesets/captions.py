from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from grison.markdown.refscan import scan_refs


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
            f"conflicting captions {list(self.captions)!r} across {', '.join(self.docs)} (REF-004)"
        )


@dataclass(frozen=True)
class CaptionTooLong:
    """One file whose single, non-conflicting resolved caption is longer than the
    remote's own caption field allows (REF-010, when the calling adapter has such
    a limit at all — see :func:`collect_captions`'s ``caption_max_chars``)
    slipping past the validator — the exact same defense-in-depth reasoning as
    :class:`CaptionConflict` (module docstring): the file gets no entry in
    :func:`collect_captions`'s other return value (degrades to "no local caption
    opinion" — mirrors the remote, never pushed, so the file's own upload/push
    still proceeds with whatever caption it already had), and
    :func:`sync_fileset` turns this into its own ``failed`` event/
    :class:`~grison.engine.model.Plan` too."""

    name: str
    caption: str
    doc: str
    max_chars: int

    @property
    def detail(self) -> str:
        return (
            f"{self.doc}: caption is {len(self.caption)} characters, over the "
            f"{self.max_chars}-character limit (REF-010)"
        )


def collect_captions(
    doc_bodies: dict[PurePosixPath, str],
    *,
    folder: PurePosixPath,
    caption_max_chars: int | None = None,
) -> tuple[dict[str, ReferenceCaption], list[CaptionConflict], list[CaptionTooLong]]:
    """Scan every document in ``doc_bodies`` for embeds whose path resolves inside
    ``folder`` (``<folder-name>/name`` or, one level down, ``../<folder-name>/name``
    — callers pass whichever spellings their own REF rule accepts), keyed by
    filename. Returns ``(captions, conflicts, too_long)``: a name whose referencing
    documents disagree on a non-empty caption is left OUT of ``captions`` and
    reported in ``conflicts`` instead, and — only when the caller passes a real
    ``caption_max_chars`` (this module has no opinion of its own on any one
    remote's limit; the caller's adapter supplies it, or omits it for a remote
    with no such limit) — a name whose single agreed-on caption is over that limit
    is left OUT of ``captions`` and reported in ``too_long`` instead. Both are
    problems the validator should already have rejected (REF-004/REF-010; defense
    in depth, not primary enforcement — see module docstring), never guessed at
    and never raised past the one file each concerns."""
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
    too_long: list[CaptionTooLong] = []
    for name, entries in by_name.items():
        captions = {c for _d, c, _t in entries if c}
        descriptions = {t for _d, _c, t in entries if t}
        if len(captions) > 1:
            doc_by_caption: dict[str, PurePosixPath] = {}
            for doc_path, c, _t in entries:
                if c and c not in doc_by_caption:
                    doc_by_caption[c] = doc_path
            conflicts.append(
                CaptionConflict(
                    name=name,
                    captions=tuple(sorted(captions)),
                    docs=tuple(str(doc_by_caption[c]) for c in sorted(captions)),
                )
            )
            continue
        caption = next(iter(captions), "")
        if caption_max_chars is not None and len(caption) > caption_max_chars:
            doc_path = next(d for d, c, _t in entries if c == caption)
            too_long.append(
                CaptionTooLong(
                    name=name, caption=caption, doc=str(doc_path), max_chars=caption_max_chars
                )
            )
            continue
        description = next(iter(descriptions), "") if len(descriptions) <= 1 else ""
        out[name] = ReferenceCaption(
            caption=caption, description=description, has_opinion=bool(captions)
        )
    return out, conflicts, too_long


def _match_folder(ref_path: str, *, folder_name: str, prefix: str) -> str | None:
    if ref_path.startswith(prefix):
        return ref_path[len(prefix) :]
    alt_prefix = f"../{folder_name}/"
    if ref_path.startswith(alt_prefix):
        return ref_path[len(alt_prefix) :]
    return None


# --- referencing-document canonicalization helpers --------------------------

_EMBED_LINE_RE = re.compile(r'^(!\[)([^\]]*)(\]\()([^)\s]+)(?:\s+"([^"]*)")?(\)\s*)$', re.MULTILINE)


def strip_embed_captions(md: str) -> str:
    """A referencing document's OWN canonical payload must never include a
    file-set embed's caption/title (they belong to the remote row, not the
    document — see the module docstring): otherwise rewriting an alt on pull
    would change the document's content hash and make it look locally edited.
    Blanks every embed's alt/title text, keeping the path (identity) intact.
    :func:`canonical_prose`/:func:`canonical_remote_prose` fold this same
    exclusion into their own single-pass substitution (:func:`_substitute_ref_
    identity`, which drops alt/title AND replaces the path) rather than
    calling this directly; kept as its own named, independently useful/
    testable transform (the caption-blanking half in isolation)."""
    return _EMBED_LINE_RE.sub(lambda m: f"{m.group(1)}{m.group(3)}{m.group(4)}{m.group(6)}", md)


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
            name = path[len(prefix) :]
        elif path.startswith(alt_prefix):
            name = path[len(alt_prefix) :]
        if name is None or name not in resolved:
            return m.group(0)
        caption, description = resolved[name]
        title = f' "{description}"' if description else ""
        return f"{m.group(1)}{caption}{m.group(3)}{path}{title}{m.group(6)}"

    return _EMBED_LINE_RE.sub(_sub, md)
