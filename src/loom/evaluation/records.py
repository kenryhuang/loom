"""Compatibility exports for trace record normalization."""

from loom.trace_analysis.records import load_normalized_events, normalize_record
from loom.trace_analysis.schemas import NormalizedEvent, TraceIngestResult

__all__ = ["NormalizedEvent", "TraceIngestResult", "load_normalized_events", "normalize_record"]
