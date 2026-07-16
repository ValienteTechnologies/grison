from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from grison.scanners.ir import Finding, Severity


@dataclass
class ImportOptions:
    severity_filter: set[Severity] | None = None  # None = all severities
    include_plugins: list[str] = field(default_factory=list)
    exclude_plugins: list[str] = field(default_factory=list)
    min_qod: int = 0          # OpenVAS: minimum quality-of-detection threshold
    fmt: str = "xml"          # Nmap: "xml" | "grepable"
    no_snoozed: bool = False  # Nessus: skip snoozed findings


class Scanner(ABC):
    name: ClassVar[str]   # CLI subcommand slug, e.g. "burp"
    label: ClassVar[str]  # Human display name, e.g. "Burp Suite"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "__abstractmethods__", None):
            if not isinstance(cls.__dict__.get("name"), str):
                raise TypeError(
                    f"{cls.__name__} must define a class-level `name: ClassVar[str]` (CLI slug)"
                )
            if not isinstance(cls.__dict__.get("label"), str):
                raise TypeError(
                    f"{cls.__name__} must define a class-level "
                    "`label: ClassVar[str]` (display name)"
                )

    @abstractmethod
    def parse(self, data: bytes, opts: ImportOptions) -> list[Finding]: ...

    @staticmethod
    def sort_by_severity(findings: list[Finding]) -> list[Finding]:
        """Sort findings most-severe first (was copy-pasted across every parser)."""
        order = list(Severity)
        return sorted(findings, key=lambda f: order.index(f.severity), reverse=True)

    @staticmethod
    def max_severity(a: Severity, b: Severity) -> Severity:
        """Return the more severe of two (for merging occurrences of the same finding)."""
        order = list(Severity)
        return a if order.index(a) >= order.index(b) else b

    def _severity_allowed(self, severity: Severity, opts: ImportOptions) -> bool:
        if opts.severity_filter is None:
            return True
        return severity in opts.severity_filter

    def _plugin_allowed(self, plugin_id: str, opts: ImportOptions) -> bool:
        if opts.include_plugins and plugin_id not in opts.include_plugins:
            return False
        if opts.exclude_plugins and plugin_id in opts.exclude_plugins:
            return False
        return True
