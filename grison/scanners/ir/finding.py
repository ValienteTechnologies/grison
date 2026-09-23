from __future__ import annotations

from dataclasses import dataclass, field

from .severity import Severity


@dataclass
class ScanFinding:
    title: str
    plugin_id: str  # scanner-native ID used for deduplication
    severity: Severity

    cvss_vector: str = ""
    cwe: str = ""

    # Ghostwriter HTML fields
    description: str = ""
    impact: str = ""
    mitigation: str = ""
    references: str = ""
    replication_steps: str = ""
    # host_/network_detection_techniques dropped on salvage — never populated by any
    # scanner and dead (~0%) in the GW corpus. finding_guidance (OpenVAS's vuldetect)
    # dropped too, 2026-09: workspace format v2 has no field to carry it, and it was
    # never read by grison.markdown.mapping.

    # Populated by scanners, rendered into replication_steps before upload
    affected_components: list[str] = field(default_factory=list)

    # Metadata — not sent to Ghostwriter
    tags: list[str] = field(default_factory=list)

    # Free-text notes a parser attaches about its own extraction (e.g. "a CVSS v4
    # vector was present but dropped") — ir_to_finding (grison.markdown.mapping)
    # turns each into a "finding <title>: <note>" warning. Never sent to Ghostwriter.
    notes: list[str] = field(default_factory=list)
