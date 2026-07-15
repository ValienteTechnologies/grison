"""Phase-4 tests: Finding ⇄ markdown-document round-trip is identity."""

from __future__ import annotations

import pytest

from grison.markdown.document import (
    DocumentError,
    finding_to_markdown,
    markdown_to_finding,
)
from grison.model import Finding


def _library() -> Finding:
    return Finding.model_validate(
        {
            "grison": {"tier": "library", "gw": {"id": 42}},
            "severity": "high",
            "finding_type": "web",
            "cvss": {"vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
            "cwe": ["CWE-79"],
            "tags": ["xss", "ATT&CK:T1059"],
            "title": "Reflected XSS",
            "description": "A **reflected** cross-site scripting flaw in `q`.",
            "impact": "- Session theft\n- Account takeover",
            "mitigation": "Encode output; set `HttpOnly`.",
            "replication_steps": "- Send `?q=<script>`\n- Observe reflection",
            "references": "- **CWE-79:** <https://cwe.mitre.org/data/definitions/79.html>",
        }
    )


def _instance_with_evidence() -> Finding:
    return Finding.model_validate(
        {
            "grison": {
                "tier": "instance",
                "gw": {"id": 183, "report_id": 7},
                "synced": {"hash": "sha256:abc", "at": "2026-07-14T12:00:00Z"},
            },
            "severity": "medium",
            "finding_type": "mobile",
            "cwe": ["CWE-16"],
            "affected_entities": "https://app.example/\nhttps://api.example/",
            "evidence": [
                {
                    "file": "evidence/shell.png",
                    "caption": "Reverse shell",
                    "friendly_name": "reverse-shell",
                    "gw": {"id": 38, "hash": "sha256:def"},
                }
            ],
            "title": "Weak TLS",
            "description": "Uses deprecated ciphers.",
            "impact": "",
            "mitigation": "Disable TLS 1.0/1.1.",
            "replication_steps": "",
            "references": "",
        }
    )


@pytest.mark.parametrize("factory", [_library, _instance_with_evidence])
def test_roundtrip_identity(factory) -> None:  # type: ignore[no-untyped-def]
    f = factory()
    md = finding_to_markdown(f)
    assert markdown_to_finding(md) == f


def test_markdown_shape() -> None:
    md = finding_to_markdown(_library())
    assert md.startswith("---\n")
    assert "\n# Reflected XSS\n" in md
    for header in ("## Description", "## Impact", "## Mitigation",
                   "## Replication Steps", "## References"):
        assert header in md
    # body fields do NOT leak into frontmatter
    frontmatter = md.split("---\n", 2)[1]
    assert "Reflected XSS" not in frontmatter
    assert "reflected" not in frontmatter


def test_frontmatter_omits_empty_and_none() -> None:
    md = finding_to_markdown(_library())
    frontmatter = md.split("---\n", 2)[1]
    assert "affected_entities" not in frontmatter  # None on this library finding
    assert "evidence" not in frontmatter  # empty list pruned
    assert "report_id" not in frontmatter  # None


def test_errors() -> None:
    with pytest.raises(DocumentError, match="frontmatter"):
        markdown_to_finding("# Title\n\nno frontmatter")
    with pytest.raises(DocumentError, match="title"):
        markdown_to_finding("---\ngrison:\n  tier: library\nseverity: low\n"
                            "finding_type: host\n---\n\n## Description\n\nx")
    with pytest.raises(DocumentError, match="unknown section"):
        markdown_to_finding(
            "---\ngrison:\n  tier: library\nseverity: low\nfinding_type: host\n---\n\n"
            "# T\n\n## Bogus\n\nx"
        )
