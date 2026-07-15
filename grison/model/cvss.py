"""CVSS 3.0 / 3.1 base vector parser and scorer.

Accepts well-formed ``CVSS:3.0/...`` and ``CVSS:3.1/...`` base vectors and
computes the base score per the CVSS v3.1 specification. The formula is
identical for 3.0 and 3.1 except for the final rounding step: 3.1 uses the
official "roundup" algorithm, 3.0 rounds to one decimal place.

CVSS 2.0, 4.0, and any malformed vector are rejected with :class:`CvssError`.
"""

from __future__ import annotations

from dataclasses import dataclass

_SUPPORTED_VERSIONS = ("3.0", "3.1")

_BASE_METRIC_ORDER = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")

_BASE_METRIC_VALUES: dict[str, set[str]] = {
    "AV": {"N", "A", "L", "P"},
    "AC": {"L", "H"},
    "PR": {"N", "L", "H"},
    "UI": {"N", "R"},
    "S": {"U", "C"},
    "C": {"H", "L", "N"},
    "I": {"H", "L", "N"},
    "A": {"H", "L", "N"},
}

# Temporal + Environmental metrics: allowed to appear, ignored for the base
# score, but still validated against their legal value sets.
_OPTIONAL_METRIC_VALUES: dict[str, set[str]] = {
    "E": {"X", "H", "F", "P", "U"},
    "RL": {"X", "U", "W", "T", "O"},
    "RC": {"X", "C", "R", "U"},
    "CR": {"X", "H", "M", "L"},
    "IR": {"X", "H", "M", "L"},
    "AR": {"X", "H", "M", "L"},
    "MAV": {"X", "N", "A", "L", "P"},
    "MAC": {"X", "L", "H"},
    "MPR": {"X", "N", "L", "H"},
    "MUI": {"X", "N", "R"},
    "MS": {"X", "U", "C"},
    "MC": {"X", "H", "L", "N"},
    "MI": {"X", "H", "L", "N"},
    "MA": {"X", "H", "L", "N"},
}

_AV_WEIGHTS = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC_WEIGHTS = {"L": 0.77, "H": 0.44}
_UI_WEIGHTS = {"N": 0.85, "R": 0.62}
_CIA_WEIGHTS = {"H": 0.56, "L": 0.22, "N": 0.0}
_PR_WEIGHTS_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_WEIGHTS_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.5}


class CvssError(ValueError):
    """Raised when a CVSS vector string is malformed or unsupported."""


@dataclass(frozen=True)
class CvssVector:
    raw: str
    version: str
    metrics: dict[str, str]
    base_score: float


def _roundup_31(value: float) -> float:
    """Official CVSS v3.1 spec Appendix A "roundup" algorithm."""
    int_value = int(round(value * 100000))
    if int_value % 10000 == 0:
        return int_value / 100000
    return (int_value // 10000 + 1) / 10


def _round_30(value: float) -> float:
    """CVSS v3.0 style round-to-one-decimal (round half up)."""
    return round(value + 1e-9, 1)


def _compute_base_score(metrics: dict[str, str], version: str) -> float:
    av = _AV_WEIGHTS[metrics["AV"]]
    ac = _AC_WEIGHTS[metrics["AC"]]
    ui = _UI_WEIGHTS[metrics["UI"]]
    c = _CIA_WEIGHTS[metrics["C"]]
    i = _CIA_WEIGHTS[metrics["I"]]
    a = _CIA_WEIGHTS[metrics["A"]]

    scope_changed = metrics["S"] == "C"
    pr_weights = _PR_WEIGHTS_CHANGED if scope_changed else _PR_WEIGHTS_UNCHANGED
    pr = pr_weights[metrics["PR"]]

    isc_base = 1 - ((1 - c) * (1 - i) * (1 - a))
    if scope_changed:
        impact = 7.52 * (isc_base - 0.029) - 3.25 * (isc_base - 0.02) ** 15
    else:
        impact = 6.42 * isc_base

    if impact <= 0:
        return 0.0

    exploitability = 8.22 * av * ac * pr * ui

    if scope_changed:
        raw_score = min(1.08 * (impact + exploitability), 10)
    else:
        raw_score = min(impact + exploitability, 10)

    if version == "3.1":
        return _roundup_31(raw_score)
    return _round_30(raw_score)


def parse_cvss(vector: str) -> CvssVector:
    """Parse and validate a CVSS 3.0/3.1 base vector string.

    Raises :class:`CvssError` if the vector is malformed, has an unsupported
    version, is missing a required base metric, has a duplicate metric, or
    contains an unknown metric key / illegal value.
    """
    if not vector.startswith("CVSS:"):
        raise CvssError(f"not a CVSS vector: missing 'CVSS:' prefix in {vector!r}")

    parts = vector.split("/")
    version = parts[0][len("CVSS:") :]
    if version not in _SUPPORTED_VERSIONS:
        raise CvssError(
            f"unsupported CVSS version {version!r} in {vector!r}; "
            f"only {' and '.join(_SUPPORTED_VERSIONS)} are supported"
        )

    seen: dict[str, str] = {}
    for part in parts[1:]:
        if not part:
            raise CvssError(f"empty metric segment in vector {vector!r}")
        if ":" not in part:
            raise CvssError(f"malformed metric segment {part!r} in vector {vector!r}")

        key, _, value = part.partition(":")
        if key in seen:
            raise CvssError(f"duplicate metric {key!r} in vector {vector!r}")

        if key in _BASE_METRIC_VALUES:
            allowed = _BASE_METRIC_VALUES[key]
        elif key in _OPTIONAL_METRIC_VALUES:
            allowed = _OPTIONAL_METRIC_VALUES[key]
        else:
            raise CvssError(f"unknown metric {key!r} in vector {vector!r}")

        if value not in allowed:
            raise CvssError(f"illegal value {value!r} for metric {key!r} in vector {vector!r}")

        seen[key] = value

    missing = [m for m in _BASE_METRIC_ORDER if m not in seen]
    if missing:
        raise CvssError(f"missing required base metric(s) {missing} in vector {vector!r}")

    base_metrics = {m: seen[m] for m in _BASE_METRIC_ORDER}
    base_score = _compute_base_score(base_metrics, version)

    return CvssVector(raw=vector, version=version, metrics=base_metrics, base_score=base_score)


def is_valid_cvss(vector: str) -> bool:
    """Return True iff ``parse_cvss(vector)`` succeeds."""
    try:
        parse_cvss(vector)
    except CvssError:
        return False
    return True
