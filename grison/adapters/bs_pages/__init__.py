"""BookStack wiki pages, on the sync engine (ENGINE.md Adapter protocol; BRIEF D4).

A page's book/chapter come from its directory only (D4) — the local document
(:mod:`grison.formats.wiki`) carries just ``title``/``priority``/``tags``/body; this
package attaches ``book``/``chapter`` (directory-derived slugs) when it scans a file,
so :meth:`~.adapter.BsPageAdapter.canonical_local` has everything it needs without a
path argument (the :class:`~grison.engine.adapter.Adapter` protocol doesn't pass one).

No converter for prose: BookStack pages are markdown-native, mirrored verbatim (same
as the pre-engine module this replaces). ONE exception (D9/BRIEF task C): a gallery
image line, ``![caption](images/x.png)`` at the book root or ``![caption](../images/
x.png)`` inside a chapter (REF-007's two accepted spellings), is translated to/from
BookStack's own absolute gallery URL — the only spelling that actually renders an
image in BookStack, and the one it stores verbatim in ``markdown``. The translation
happens ONLY where a live gallery lookup (``ctx``) is available:
:meth:`~.adapter.BsPageAdapter.fetch_remote`/:meth:`~.adapter.BsPageAdapter.refetch`
translate URL -> local path once, into ``data["markdown"]``, so
:meth:`~.adapter.BsPageAdapter.canonical_remote`/:meth:`~.adapter.BsPageAdapter.
render_local` (which get no ``ctx`` at all — the
:class:`~grison.engine.adapter.Adapter` protocol doesn't pass one to those) can just
use it verbatim, already in the SAME vocabulary
:meth:`~.adapter.BsPageAdapter.canonical_local` compares against (the local file's
own, as-authored body); :meth:`~.adapter.BsPageAdapter.create`/:meth:`~.adapter.
BsPageAdapter.update`/:meth:`~.adapter.BsPageAdapter.restore` translate local path ->
URL right before sending. See :mod:`grison.adapters.bs_images` for the ``images/``
folder <-> gallery row mechanism this depends on.

Split one file per concern (round 3 dedup — this used to be one 614-line
``grison/adapters/bs_pages.py``): :mod:`.normalize` (remote-row normalisation + tag
conversion), :mod:`.gallery` (the gallery image line <-> absolute URL translation),
:mod:`.adapter` (:class:`~.adapter.BsPageAdapter` itself). :class:`~.adapter.
BsPageAdapter` is re-exported here unchanged.
"""

from __future__ import annotations

from .adapter import BsPageAdapter, PageDoc

__all__ = ["BsPageAdapter", "PageDoc"]
