"""The parse pipeline: raw scanner file(s) → detect → parse → map → file sink.

A path argument resolves to a set of scanner files (a dir → its files, a file →
itself). Each file is sniffed for its scanner (``--scanner`` overrides); an
unrecognized file is skipped with a warning. Raw files are read in place — grison
writes only to the output dir (``findings/inbox/`` by default), never houses the raw.
One bad finding fails only itself, not the batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from grison.errors import GrisonError
from grison.formats.finding import FindingDoc
from grison.markdown import default_finding_type, ir_to_finding
from grison.model import FindingType
from grison.scanners import ImportOptions, detect, scanner_for
from grison.scanners.ir import parse_severity_filter
from grison.sinks.file_sink import FileSink, SinkResult


class ParsePathNotFound(GrisonError):
    """A ``grison parse`` path argument doesn't exist on disk.

    Raised before any path is resolved to files, any file is read, or anything is
    written — the CLI turns this into exit 2 ("could not run"), never 0 or 1."""


@dataclass
class ParseSummary:
    files_parsed: dict[str, int] = field(default_factory=dict)  # scanner -> file count
    skipped_files: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    findings: list[FindingDoc] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # dedupe key per finding (parallel)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # per-finding validation failures
    sink: SinkResult | None = None


def _resolve(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(x for x in p.iterdir() if x.is_file()))
        else:
            files.append(p)
    return files


def run_parse(
    paths: list[Path],
    out_dir: Path,
    *,
    scanner: str | None = None,
    finding_type: FindingType | None = None,
    min_severity: str | None = None,
    dry_run: bool = False,
) -> ParseSummary:
    """Parse scanner exports into markdown proto-instances under ``out_dir``.

    A ``paths`` entry that doesn't exist on disk is "could not run", not "nothing to
    do": :class:`ParsePathNotFound` is raised for the whole batch before any path is
    resolved to files, so a typo never comes back as a quiet, empty success.
    """
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise ParsePathNotFound("no such file or directory: " + ", ".join(str(p) for p in missing))

    summary = ParseSummary()
    sev_filter = parse_severity_filter(min_severity) if min_severity else None

    for f in _resolve(paths):
        name = scanner or detect(f)
        if name is None:
            reason = "unrecognized scanner type"
            summary.skipped_files.append((f, reason))
            summary.errors.append(f"{f.name}: {reason}")
            continue
        cls = scanner_for(name)
        if cls is None:
            reason = f"unknown scanner {name!r}"
            summary.skipped_files.append((f, reason))
            summary.errors.append(f"{f.name}: {reason}")
            continue
        try:
            ir_list = cls().parse(f.read_bytes(), ImportOptions(severity_filter=sev_filter))
        except Exception as e:  # noqa: BLE001 — one bad file must not kill the batch
            summary.skipped_files.append((f, f"parse error: {e}"))
            continue

        summary.files_parsed[name] = summary.files_parsed.get(name, 0) + 1
        ftype = finding_type or default_finding_type(name)
        for ir in ir_list:
            try:
                res = ir_to_finding(ir, finding_type=ftype)
            except ValidationError as e:
                summary.errors.append(f"{f.name}: {ir.title!r}: {e}")
                continue
            summary.findings.append(res.finding)
            summary.keys.append(ir.plugin_id)
            summary.warnings.extend(res.warnings)

    summary.sink = FileSink(out_dir).write(summary.findings, keys=summary.keys, dry_run=dry_run)
    summary.errors.extend(summary.sink.errors)  # sink failures must reach the exit code
    return summary
