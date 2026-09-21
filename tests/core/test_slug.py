"""The one shared slugify (grison.adapters._slug): transliteration + fallback."""

from __future__ import annotations

import pytest

from grison.adapters._gw_common import slugify as gw_slugify
from grison.adapters._slug import slugify
from grison.sinks import slugify as inbox_slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Sızma Testi Raporu", "sizma-testi-raporu"),
        ("Tanımlar", "tanimlar"),
        ("Kılavuzlar & Örnekler", "kilavuzlar-ornekler"),
        ("Açıklamalar", "aciklamalar"),
        ("Größe & Maß", "grosse-mass"),
        ("Ærø – Øresund", "aero-oresund"),
        ("  Weak   TLS  Ciphers!  ", "weak-tls-ciphers"),
        ("naïve café", "naive-cafe"),
    ],
)
def test_slugify_transliterates_to_ascii(title: str, expected: str) -> None:
    assert slugify(title, fallback="x") == expected


def test_slugify_falls_back_when_nothing_survives() -> None:
    assert slugify("", fallback="report") == "report"
    assert slugify("!!! ???", fallback="page") == "page"
    assert gw_slugify("") == "report"
    assert inbox_slugify("") == "finding"


def test_every_wrapper_is_the_same_slugify() -> None:
    assert gw_slugify("Sızma Testi") == inbox_slugify("Sızma Testi") == "sizma-testi"
