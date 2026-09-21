from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from grison.markdown.converter import ConverterError, html_to_md
from grison.markdown.refs import LocalRef, RemoteRef
from grison.markdown.refscan import FoundRef, ref_spans, scan_refs


class ResolvesEmbeds(Protocol):
    def to_remote_id(self, path: str) -> int | None: ...


# Folding was ONCE done with a hand-rolled regex over the whole text, matching
# by SHAPE alone and excluding a match by checking whether its own matched TEXT
# was contained in one of `~grison.markdown.refscan.code_spans`' results — text
# containment, not position: a real reference whose exact text ALSO happened to
# appear inside a code fence elsewhere in the document excluded BOTH occurrences
# (the regex has no notion of "this specific occurrence, at this position");
# nested `![alt](inner)](outer)` misparsed (the caption group has no concept of
# bracket depth, so it closed at the INNER `]`, leaving a dangling `](outer)`);
# and an escaped `\]` in a caption never matched at all (same reason). All three
# are the SAME root cause — re-deriving "what a reference looks like" with a
# second parser instead of asking the real one — so this now folds by POSITION
# from the one real parse: :func:`~grison.markdown.refscan.ref_spans` gives the
# exact character span of every reference :func:`~grison.markdown.refscan.
# scan_refs` reports, in the same order (see that module for how — a
# bracket-depth/escape-aware scan restricted to real paragraph/heading blocks,
# so a fenced copy is structurally invisible and an inline code-span lookalike
# is excluded by real position, not text).
def _is_grison_reference(ref: FoundRef) -> bool:
    """The SAME rule :mod:`grison.markdown.converter`'s PUSH side uses (its
    ``_render_md_link``): an embed is always a reference (this vocabulary has
    no concept of a non-evidence image); a plain link is a cross-reference
    ONLY when its destination literally starts with ``evidence/`` — an
    ordinary external link, a ``mailto:``, an anchor, or any other destination
    is never a grison reference and must be left byte-identical on both the
    local and remote canonical payload (the bug this classification fixes: a
    library finding's external link, e.g. an OWASP citation, used to be folded
    into ``[unresolved](unresolved)`` on the LOCAL side only — the remote side
    never touches an ordinary ``<a>`` at all, see ``canonical_remote_prose`` —
    so the record pushed forever)."""
    if ref.kind == "embed":
        return True
    return ref.path.startswith("evidence/")


def _substitute_ref_identity(md: str, token_for: Callable[[str], str]) -> str:
    """Fold BOTH an embed's and a cross-reference's identity into ``md`` — the
    single mechanism :func:`canonical_prose` uses for the "replace every
    reference's destination with its currently-resolved remote id" half of its
    job (module docstring, "Cross-references (D1, restated)"):

      - embed (``![caption](path "title")``) -> ``![](<token_for(path)>)`` —
        alt/title dropped (:func:`strip_embed_captions`'s own reason: they
        belong to the remote row, not the document).
      - cross-reference (``[caption](path "title")``) -> ``[<token_for(path)>]
        (<token_for(path)>)`` — the SAME shape :func:`canonical_remote_prose`
        reaches independently (via ``html_to_md``'s own resolver contract: a
        cross-reference's native HTML span carries no text of its own, so its
        display text is always synthesised from the resolved evidence, never
        preserved — see :mod:`grison.markdown.converter`'s module docstring),
        so the two sides are directly comparable — never left as the
        author's own caption/path, which would leave LOCAL's payload
        permanently disagreeing with REMOTE's (the bug this function fixes).

    ONLY a reference :func:`_is_grison_reference` recognises is folded; every
    other markdown link/image (an external URL, a mailto:, an anchor, a plain
    internal doc link) is left byte-identical, on both sides, unconditionally —
    see :func:`_is_grison_reference`'s own docstring.

    :func:`~grison.markdown.refscan.scan_refs` and :func:`~grison.markdown.
    refscan.ref_spans` must report the SAME COUNT for ``md`` (they're the same
    parse, matched by ordinal index — see that module) — a mismatch means a
    construct this module's simpler span-scanner and the real parser disagree
    about, which today is exactly one shape: an image nested inside a link's
    text (``[![alt](inner)](outer)``). Rejected outright (never folded as
    "only the outer link", never guessed at) — the converter's own "image not
    alone in its paragraph" rule already refuses this shape at push, so
    canonicalizing it silently would only ever describe a record that can
    never actually be pushed as written."""
    refs = scan_refs(md)
    spans = ref_spans(md)
    if len(refs) != len(spans):
        raise ConverterError(
            "unsupported markdown: a reference-shaped construct could not be resolved "
            "unambiguously (e.g. an image nested inside a link's text — an embed must "
            "be its own block, never nested inside a link's caption)"
        )
    out: list[str] = []
    pos = 0
    normalized = md.replace("\r\n", "\n")
    for ref, (start, end) in zip(refs, spans, strict=True):
        if not _is_grison_reference(ref):
            continue
        if start > pos:
            out.append(normalized[pos:start])
        token = token_for(ref.path)
        out.append(f"![]({token})" if ref.kind == "embed" else f"[{token}]({token})")
        pos = end
    out.append(normalized[pos:])
    return "".join(out)


def canonical_prose(md: str, resolver: ResolvesEmbeds) -> dict[str, Any]:
    """Canonical payload for one LOCAL prose field/section that may embed or
    cross-reference file-set references: the caption-stripped text, with every
    reference's AUTHORED PATH replaced by its CURRENTLY-resolved remote id
    (looked up through the live index/evidence rows ``resolver`` wraps) — never
    left as the path itself, which stays the stable text ``evidence/x.png`` even
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
    # .strip() on BOTH sides (here and canonical_remote_prose): every local format
    # parser strips its prose (finding sections, narrative body, note body, page
    # body), so the remote form must too, or a record whose stored HTML ends in an
    # empty paragraph/heading classifies as "edited" right after a clean pull.
    return {
        "text": _substitute_ref_identity(
            md.strip(), lambda path: _id_token(resolver.to_remote_id(path))
        )
    }


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

    name_to_id: Mapping[str, int] | Callable[[], Mapping[str, int]] = field(default_factory=dict)

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
    html: str,
    *,
    headings: bool = False,
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
    return {"text": md.strip()}  # parity with canonical_prose — see its comment
