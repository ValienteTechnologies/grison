from __future__ import annotations

from grison.scanners.ir import ScanFinding

from .base import ImportOptions, RefusedInput, Scanner

# Nmap is recon output (open ports, not vulnerabilities): no finding this parser
# could construct from it would carry a real severity or remediation, and the
# vendored XML/grepable walk that used to synthesize an INFO "Open Ports" finding
# per host was reclassified out of scope — see RefusedInput's docstring. Detection
# (grison.scanners.detect) still recognises an nmap export so this message stays
# specific instead of falling back to "unrecognized scanner type".
_REFUSAL = "nmap is recon output, not findings; inventory support is pending"


class NmapScanner(Scanner):
    name = "nmap"
    label = "Nmap"

    def parse(self, data: bytes, opts: ImportOptions) -> list[ScanFinding]:
        raise RefusedInput(_REFUSAL)
