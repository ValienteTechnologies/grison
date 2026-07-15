"""Scanner source adapters — vendored parsers plus a root-sniff auto-detector.

Eight parsers (Burp, Nessus, Nmap, OpenVAS, Qualys, ZAP, SSLyze, Acunetix) turn
raw scanner exports into the :mod:`grison.scanners.ir` dataclass. ``detect()``
sniffs a file to pick its parser so ``grison parse`` needs no ``--scanner`` arg.
"""

from __future__ import annotations

from grison.scanners.acunetix import AcunetixScanner
from grison.scanners.base import ImportOptions, Scanner
from grison.scanners.burp import BurpScanner
from grison.scanners.detect import detect, detect_bytes
from grison.scanners.nessus import NessusScanner
from grison.scanners.nmap import NmapScanner
from grison.scanners.openvas import OpenVASScanner
from grison.scanners.qualys import QualysScanner
from grison.scanners.sslyze import SslyzeScanner
from grison.scanners.zap import ZapScanner

# All eight parsers, keyed by their CLI slug (Scanner.name).
SCANNERS: list[type[Scanner]] = [
    AcunetixScanner,
    BurpScanner,
    NessusScanner,
    NmapScanner,
    OpenVASScanner,
    QualysScanner,
    SslyzeScanner,
    ZapScanner,
]

BY_NAME: dict[str, type[Scanner]] = {s.name: s for s in SCANNERS}


def scanner_for(name: str) -> type[Scanner] | None:
    """Look up a parser class by its slug (for ``--scanner X`` and ``detect``)."""
    return BY_NAME.get(name)


__all__ = [
    "BY_NAME",
    "SCANNERS",
    "AcunetixScanner",
    "BurpScanner",
    "ImportOptions",
    "NessusScanner",
    "NmapScanner",
    "OpenVASScanner",
    "QualysScanner",
    "Scanner",
    "SslyzeScanner",
    "ZapScanner",
    "detect",
    "detect_bytes",
    "scanner_for",
]
