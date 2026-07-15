"""Sinks: the file sink and the parse pipeline behind the ports."""

from __future__ import annotations

from grison.sinks.file_sink import FileSink, SinkResult, slugify
from grison.sinks.pipeline import ParseSummary, run_parse

__all__ = ["FileSink", "ParseSummary", "SinkResult", "run_parse", "slugify"]
