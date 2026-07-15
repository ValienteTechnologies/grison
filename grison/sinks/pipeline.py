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

from grison.markdown import default_finding_type, ir_to_finding
from grison.model import Finding, FindingType
from grison.scanners import ImportOptions, detect, scanner_for
from grison.scanners.ir import parse_severity_filter
from grison.sinks.file_sink import FileSink, SinkResult


@dataclass
class ParseSummary:
    files_parsed: dict[str, int] = field(default_factory=dict)  # scanner -> file count
    skipped_files: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    findings: list[Finding] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # dedupe key per finding (parallel)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # per-finding validation failures
    sink: SinkResult | None = None


def _resolve(paths: list[Path], summary: ParseSummary) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(x for x in p.iterdir() if x.is_file()))
        elif p.is_file():
            files.append(p)
        else:
            summary.skipped_files.append((p, "no such file or directory"))
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
    """Parse scanner exports into markdown proto-instances under ``out_dir``."""
    summary = ParseSummary()
    sev_filter = parse_severity_filter(min_severity) if min_severity else None

    for f in _resolve(paths, summary):
        name = scanner or detect(f)
        if name is None:
            summary.skipped_files.append((f, "unrecognized scanner type"))
            continue
        cls = scanner_for(name)
        if cls is None:
            summary.skipped_files.append((f, f"unknown scanner {name!r}"))
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

    summary.sink = FileSink(out_dir).write(
        summary.findings, keys=summary.keys, dry_run=dry_run
    )
    summary.errors.extend(summary.sink.errors)  # sink failures must reach the exit code
    return summary
