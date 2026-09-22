"""Ghostwriter's own evidence-upload limits, in exactly one place.

Real Ghostwriter (>= 7.2, confirmed against ``ghostwriter/reporting/validators.py``
and ``ghostwriter/reporting/models.py`` — both read-only references, never imported)
rejects an evidence upload two ways relevant here:

- ``EVIDENCE_ALLOWED_EXTENSIONS`` (``validators.py``) — a Django
  ``FileExtensionValidator`` on ``Evidence.document`` restricts uploads to a fixed
  extension allow-list. Anything else fails server-side with
  ``filename: File extension ".<ext>" is not allowed``.
- ``Evidence.caption`` (``models.py``) is a ``CharField(max_length=255)``. Anything
  longer fails server-side with
  ``caption: Ensure this value has at most 255 characters (it has N).``
  (``Evidence.friendly_name`` has the same 255-char ``max_length``, but grison never
  lets a user author it — it's always derived from the local filename stem, itself
  already bounded well under 255 bytes by REF-008 — so there is nothing to validate
  there. ``Evidence.description`` is an unbounded ``TextField``: no limit to enforce.)

Both limits are enforced twice in grison — offline, before any push
(:mod:`grison.validator.core.reports`'s REF-009/REF-010), and defensively at push time
(:mod:`grison.adapters.gw_evidence`, in case a caller reaches ``upload``/``restore``
having skipped validation) — from this one module, so the two checks can never drift
apart. The fake Ghostwriter server (``tests/fakes/gw_server.py``) enforces the same
two limits, with the real server's exact wording, so a test can prove the offline
check and the live rejection agree.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# ghostwriter/reporting/validators.py: EVIDENCE_ALLOWED_EXTENSIONS. Lowercase, no
# leading dot — an uploaded file's own extension is compared case-insensitively.
EVIDENCE_ALLOWED_EXTENSIONS = frozenset({"txt", "md", "log", "jpg", "jpeg", "png"})

# ghostwriter/reporting/models.py: Evidence.caption's CharField(max_length=255).
EVIDENCE_CAPTION_MAX_CHARS = 255

# REF-009's suggested replacement extension per common rejected one (used both in the
# validator's failure message and the scaffolded CLAUDE.md authoring text).
EVIDENCE_EXTENSION_SUGGESTIONS: dict[str, str] = {
    "gif": "png",
    "html": "txt",
    "htm": "txt",
    "csv": "txt",
}


def evidence_extension(filename: str) -> str:
    """``filename``'s extension, lowercased and without its leading dot (``""`` when
    there is none) — the exact string :data:`EVIDENCE_ALLOWED_EXTENSIONS` and
    :data:`EVIDENCE_EXTENSION_SUGGESTIONS` are keyed on."""
    return PurePosixPath(filename).suffix.lstrip(".").lower()


def is_allowed_evidence_extension(filename: str) -> bool:
    return evidence_extension(filename) in EVIDENCE_ALLOWED_EXTENSIONS
