from __future__ import annotations

from dataclasses import dataclass, field

from .severity import Severity


@dataclass
class Finding:
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
    # scanner and dead (~0%) in the GW corpus. finding_guidance stays (OpenVAS sets it).
    finding_guidance: str = ""

    # Populated by scanners, rendered into replication_steps before upload
    affected_components: list[str] = field(default_factory=list)

    # Metadata — not sent to Ghostwriter
    tags: list[str] = field(default_factory=list)
