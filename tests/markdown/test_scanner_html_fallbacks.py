"""Instrumentation test: how often real scanner-export HTML falls back to the
plain-text degrade instead of converting to real markdown.

Runs :func:`grison.markdown.mapping._prose_to_md` over every non-empty prose
field (``description``, ``impact``, ``mitigation``, ``replication_steps``) of
every finding produced by the real parsers over every fixture under
``tests/fixtures/scanners/<scanner>/`` — the same corpus
``tests/scanners/test_golden.py`` walks — and counts the fallbacks, grouped by
the underlying :class:`~grison.markdown.converter.ConverterError` message.

Before the item-3 HTML-structure normalisation (auto-closing ``<p>``,
unwrapping ``<div>``, rewriting ``<dl>``/``<dt>``/``<dd>`` — see
``_StructureNormalizer`` in ``grison/markdown/mapping.py``), this counted 191
fallbacks out of 1272 non-empty prose fields, 182 of them
``unclosed HTML tag: <p>`` (real-world Qualys prose routinely never closes a
single ``<P>``). After: 8, see ``_RESIDUAL`` below.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from grison.markdown.mapping import _prose_to_md
from grison.scanners import ImportOptions, RefusedInput, detect_bytes, scanner_for
from grison.scanners.detect import normalise_input

_FIX = Path(__file__).parent.parent / "fixtures" / "scanners"
_SCANNER_DIRS = ("acunetix", "burp", "nessus", "nmap", "openvas", "qualys", "sslyze", "zap")
_PROSE_FIELDS = ("description", "impact", "mitigation", "replication_steps")

# The residual after the item-3 structure normalisation — every one of these is
# a pre-existing, out-of-scope malformation the normalisation deliberately
# leaves untouched (module docstring's "before"/"after"), not a regression:
#   - 3x "mismatched closing tag: </strong>" (Qualys dojo-Qualys_Sample_Report.xml):
#     a <B> spans across a bare, unclosed <P> boundary inside it (real, pre-
#     existing unbalanced nesting between two DIFFERENT tags — <p> is only ever
#     auto-closed when it is the innermost open element; here <b>/<strong> is).
#   - 2x "mismatched closing tag: </ul>" (same fixture): a run of bare <LI>
#     siblings with no </LI> between them at all — <li> auto-closing <li> is
#     out of this item's scope (only <p> is auto-closed).
#   - 1x "unsupported HTML tag: <directory>", 1x "unsupported HTML tag: <limit>":
#     genuinely unsupported tags, deliberately left alone (module docstring).
#   - 1x "stray text directly inside <ul> (expected <li>)" (same Qualys
#     fixture): a <UL> followed directly by bare text with no <li> wrapper at
#     all — newly SURFACED (not introduced) by the <p> fix: this field also had
#     an unclosed <p> earlier that used to fail first and mask this deeper,
#     unrelated issue.
_RESIDUAL = 8


def _iter_prose_fields() -> list[tuple[str, str, str]]:
    """(fixture name, field name, raw HTML) for every non-empty prose field of
    every finding, across the full real scanner-export corpus."""
    fields: list[tuple[str, str, str]] = []
    for scanner in _SCANNER_DIRS:
        d = _FIX / scanner
        if not d.is_dir():
            continue
        for fixture in sorted(p for p in d.iterdir() if p.is_file()):
            data = normalise_input(fixture.read_bytes())
            name = detect_bytes(data)
            if name is None:
                continue
            cls = scanner_for(name)
            if cls is None:
                continue
            try:
                findings = cls().parse(data, ImportOptions())
            except RefusedInput:
                continue
            except Exception:  # noqa: BLE001 — a parse error isn't this test's concern
                continue
            for finding in findings:
                for field_name in _PROSE_FIELDS:
                    raw = getattr(finding, field_name)
                    if raw and raw.strip():
                        fields.append((fixture.name, field_name, raw))
    return fields


def test_scanner_prose_html_fallback_count_stays_at_or_below_residual() -> None:
    fields = _iter_prose_fields()
    assert len(fields) > 1000  # sanity: the corpus is really being walked

    fallback_messages: Counter[str] = Counter()
    for _fixture_name, field_name, raw in fields:
        warnings: list[str] = []
        _prose_to_md(raw, field_name, warnings)
        if not warnings:
            continue
        prefix = f"{field_name}: HTML outside GW whitelist, degraded to text ("
        (warning,) = warnings
        assert warning.startswith(prefix) and warning.endswith(")")
        fallback_messages[warning[len(prefix) : -1]] += 1

    total = sum(fallback_messages.values())
    detail = "\n".join(f"  {n}x {msg}" for msg, n in fallback_messages.most_common())
    assert total <= _RESIDUAL, (
        f"{total} fallbacks, more than the {_RESIDUAL} residual this test pins "
        f"(see module docstring) — a change regressed HTML->markdown coverage:\n{detail}"
    )
