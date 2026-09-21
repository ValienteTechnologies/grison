"""Rendering helpers shared by both converter directions for grison's
reference constructs: the "no RefResolver was given" error every call site
(either direction) that needed one but wasn't given one raises verbatim, the
unresolved-reference placeholder markdown (see
:mod:`grison.markdown.converter`'s module docstring, "Unresolved references"),
and the cross-reference link's markdown text (see the same docstring's
"Cross-reference" section).
"""

from __future__ import annotations

from collections.abc import Callable

from grison.markdown.converter.errors import ConverterError
from grison.markdown.converter.mdtext import _fence_code, _md_escape_run
from grison.markdown.converter.nodes import _report_loss
from grison.markdown.converter.refs_codec import _ref_display_text
from grison.markdown.refs import LocalRef


def _no_resolver_error(what: str, *, func: str, remedy: str) -> ConverterError:
    """The one wording for "a reference/embed construct was found but no
    RefResolver was given to resolve it" — every call site in both directions
    raises this, differing only in what was found (``what``), which top-level
    function needed the missing ``refs=`` (``func``), and what to do instead
    of passing one (``remedy``)."""
    return ConverterError(
        f"{what} but no RefResolver was given (pass refs= to {func} to resolve it, or {remedy})"
    )


def _unresolved_marker_md(marker: str, on_loss: Callable[[str], None] | None) -> str:
    """The reserved ``gw:evidence-ref:<marker>`` placeholder for a reference
    :meth:`~grison.markdown.refs.RefResolver.to_local` couldn't resolve (module
    docstring, "Unresolved references") — reports it via ``on_loss`` (the
    shared "unresolved reference: gw-evidence <marker>" wording) and fences it
    as inline code so it round-trips losslessly instead of failing the whole
    document. ``marker`` is the already-formatted ``id=N``/``name=NAME`` half."""
    _report_loss(on_loss, f"unresolved reference: gw-evidence {marker}")
    return _fence_code(f"gw:evidence-ref:{marker}")


def _cross_ref_link_md(local: LocalRef) -> str:
    """The markdown link text for a RESOLVED cross-reference — shared by the
    native ``<span data-gw-ref…>`` path and the legacy ``{{.ref name}}`` path
    (module docstring, "Cross-reference": the native span carries no text of
    its own, so this is synthesized deterministically from the resolved
    evidence, not preserved from any source)."""
    return f"[{_md_escape_run(_ref_display_text(local))}]({local.path})"
