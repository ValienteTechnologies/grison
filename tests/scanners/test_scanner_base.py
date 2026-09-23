"""Base-class helper tests: severity sort, and the shared aggregation/rendering
helpers (Aggregator, refs_to_html, collapse_whitespace) hoisted out of the
individual parsers in the parser-convergence step (grison.scanners.base)."""

from __future__ import annotations

from grison.scanners.base import (
    Aggregator,
    RawOccurrence,
    Scanner,
    collapse_whitespace,
    refs_to_html,
)
from grison.scanners.ir import ScanFinding, Severity


def _f(sev: Severity) -> ScanFinding:
    return ScanFinding(title=f"{sev.value} finding", plugin_id=sev.value, severity=sev)


def test_sort_by_severity_most_severe_first() -> None:
    unsorted = [_f(Severity.LOW), _f(Severity.CRITICAL), _f(Severity.INFO), _f(Severity.HIGH)]
    ordered = Scanner.sort_by_severity(unsorted)
    assert [f.severity for f in ordered] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.LOW,
        Severity.INFO,
    ]


def test_sort_by_severity_is_stable_within_a_level() -> None:
    a, b = _f(Severity.MEDIUM), _f(Severity.MEDIUM)
    a.title, b.title = "first", "second"
    ordered = Scanner.sort_by_severity([a, b])
    assert [f.title for f in ordered] == ["first", "second"]


def test_max_severity_returns_the_higher_of_two() -> None:
    assert Scanner.max_severity(Severity.LOW, Severity.HIGH) == Severity.HIGH
    assert Scanner.max_severity(Severity.CRITICAL, Severity.INFO) == Severity.CRITICAL


def test_max_severity_tie_returns_either_side() -> None:
    assert Scanner.max_severity(Severity.MEDIUM, Severity.MEDIUM) == Severity.MEDIUM


# --- collapse_whitespace ------------------------------------------------------


def test_collapse_whitespace_joins_newline_plus_indentation() -> None:
    # The real OpenVAS shape: an XML text node with embedded newline + indentation.
    raw = "Insufficient DH Group Strength\n                        Vulnerability"
    assert collapse_whitespace(raw) == "Insufficient DH Group Strength Vulnerability"


def test_collapse_whitespace_strips_and_is_idempotent_on_clean_text() -> None:
    assert collapse_whitespace("  Clean Title  ") == "Clean Title"
    assert collapse_whitespace("Clean Title") == "Clean Title"


# --- refs_to_html ---------------------------------------------------------------


def test_refs_to_html_empty_list_is_empty_string() -> None:
    assert refs_to_html([]) == ""


def test_refs_to_html_plain_url_becomes_a_link() -> None:
    html_out = refs_to_html(["https://example.com/advisory"])
    assert (
        html_out
        == '<ul><li><a href="https://example.com/advisory">https://example.com/advisory</a></li></ul>'
    )


def test_refs_to_html_non_url_text_stays_plain() -> None:
    assert refs_to_html(["CVE-2023-1234"]) == "<ul><li>CVE-2023-1234</li></ul>"


def test_refs_to_html_escapes_href_and_text() -> None:
    html_out = refs_to_html(["https://example.com/?a=1&b=2"])
    assert "&amp;" in html_out
    assert "&b=2" not in html_out  # bare "&" must not survive unescaped


def test_refs_to_html_pair_uses_anchor_text_not_bare_url() -> None:
    html_out = refs_to_html([("SQL injection", "https://portswigger.net/kb/issues/001")])
    assert '<a href="https://portswigger.net/kb/issues/001">SQL injection</a>' in html_out


def test_refs_to_html_pair_falls_back_to_url_when_text_blank() -> None:
    html_out = refs_to_html([("  ", "https://example.com/x")])
    assert '<a href="https://example.com/x">https://example.com/x</a>' in html_out


# --- Aggregator -----------------------------------------------------------------


def _occ(key: str, sev: Severity, component: str = "", description: str = "") -> RawOccurrence:
    return RawOccurrence(
        key=key,
        title=f"Finding {key}",
        severity=sev,
        affected_component=component,
        description=description,
    )


def test_aggregator_merges_severity_by_max_regardless_of_order() -> None:
    agg = Aggregator()
    agg.add(_occ("A", Severity.LOW))
    agg.add(_occ("A", Severity.HIGH))
    agg.add(_occ("A", Severity.MEDIUM))
    (rec,) = agg.records()
    assert rec.severity == Severity.HIGH


def test_aggregator_extends_affected_components_deduped_first_seen_order() -> None:
    agg = Aggregator()
    agg.add(_occ("A", Severity.LOW, component="host1"))
    agg.add(_occ("A", Severity.LOW, component="host2"))
    agg.add(_occ("A", Severity.LOW, component="host1"))  # duplicate, dropped
    agg.add(_occ("A", Severity.LOW, component=""))  # "" contributes nothing
    (rec,) = agg.records()
    assert rec.affected_components == ["host1", "host2"]


def test_aggregator_keeps_first_occurrences_fields_only() -> None:
    agg = Aggregator()
    agg.add(_occ("A", Severity.LOW, description="first"))
    agg.add(_occ("A", Severity.HIGH, description="second"))
    (rec,) = agg.records()
    assert rec.description == "first"


def test_aggregator_collapses_title_whitespace() -> None:
    agg = Aggregator()
    agg.add(RawOccurrence(key="A", title="Multi\n   line   title", severity=Severity.INFO))
    (rec,) = agg.records()
    assert rec.title == "Multi line title"


def test_aggregator_preserves_first_seen_key_order() -> None:
    agg = Aggregator()
    agg.add(_occ("B", Severity.INFO))
    agg.add(_occ("A", Severity.INFO))
    agg.add(_occ("B", Severity.INFO))  # repeat of B must not move it
    assert [rec.key for rec in agg.records()] == ["B", "A"]
