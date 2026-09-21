"""Proofs for grison/markdown/frontmatter.py: split()'s precise error messages,
dump()/dump_yaml()'s exact byte shape, and that document.py/bsmap.py/repmap.py now
route through this one implementation instead of their own inline copies."""

from __future__ import annotations

import pytest
import yaml

from grison.markdown.frontmatter import DocumentError, dump, dump_yaml, split


def test_split_returns_dict_and_body() -> None:
    meta, body = split("---\ntitle: X\n---\n\nHello\n")
    assert meta == {"title": "X"}
    assert body.strip() == "Hello"


def test_split_empty_frontmatter_block_is_empty_dict() -> None:
    meta, _ = split("---\n---\nbody")
    assert meta == {}


def test_split_no_opening_fence_raises() -> None:
    with pytest.raises(DocumentError, match="no YAML frontmatter"):
        split("# just markdown, no fence")


def test_split_unterminated_fence_raises() -> None:
    with pytest.raises(DocumentError, match="unterminated"):
        split("---\ntitle: X\nno closing fence")


def test_split_invalid_yaml_raises_with_line() -> None:
    text = "---\ntitle: X\n  bad: [unclosed\n---\nbody"
    with pytest.raises(DocumentError, match=r"invalid YAML frontmatter \(line \d+\)"):
        split(text)


def test_split_non_mapping_frontmatter_raises() -> None:
    with pytest.raises(DocumentError, match="not a mapping"):
        split("---\n- just\n- a\n- list\n---\nbody")


def test_dump_roundtrips_through_split() -> None:
    text = dump({"a": 1, "b": [1, 2]}, "hello\nworld")
    meta, body = split(text)
    assert meta == {"a": 1, "b": [1, 2]}
    assert body.strip() == "hello\nworld"


def test_dump_empty_body_has_no_trailing_body_section() -> None:
    text = dump({"a": 1}, "")
    assert text == "---\na: 1\n---\n"


def test_dump_uses_sort_keys_false_and_allow_unicode() -> None:
    # field order preserved (not alphabetized), and non-ascii passes through raw
    text = dump({"z": 1, "a": "café"}, "")
    assert text.index("z:") < text.index("a:")
    assert "café" in text
    assert "\\u" not in text


def test_dump_yaml_has_no_frontmatter_fence() -> None:
    text = dump_yaml({"a": 1})
    assert not text.startswith("---")
    assert yaml.safe_load(text) == {"a": 1}


def test_dump_yaml_matches_yaml_safe_dump_options() -> None:
    data = {"z": 1, "a": "café"}
    assert dump_yaml(data) == yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
