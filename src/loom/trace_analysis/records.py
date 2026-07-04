"""Trace record normalization for Loom analysis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.trace_analysis.schemas import NormalizedEvent, TraceIngestResult


def load_normalized_events(path: str | Path) -> Result:
    trace_path = Path(path)
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not read trace path",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(trace_path)},
            )
        )

    events: list[NormalizedEvent] = []
    try:
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                return err(_load_error(trace_path, "Trace record must be a JSON object", line_number=line_number))
            events.append(normalize_record(raw, line_number=line_number))
    except json.JSONDecodeError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace JSONL is malformed",
                retryable=False,
                cause={"message": str(exc), "line": exc.lineno, "column": exc.colno},
                metadata={"path": str(trace_path)},
            )
        )
    return ok(TraceIngestResult(events=tuple(events)))


def normalize_record(raw: Mapping[str, Any], *, line_number: int | None = None) -> NormalizedEvent:
    payload = _payload(raw)
    nested_trace = payload.get("trace")
    if not isinstance(nested_trace, Mapping):
        nested_trace = {}
    record_type = str(raw.get("type") or "")
    event_type = _event_type(raw, payload, record_type)
    trace_id = _first_str(raw.get("traceId"), payload.get("trace_id"), payload.get("id"), nested_trace.get("id"), raw.get("id"))
    run_id = _first_str(raw.get("runId"), payload.get("run_id"), nested_trace.get("run_id"))
    loop_id = _first_str(payload.get("loop_id"), nested_trace.get("loop_id"))
    step_number = _step_number(payload, nested_trace)
    record_id = _first_str(raw.get("id"), payload.get("id"), raw.get("hash")) or f"line-{line_number or 0}"
    return NormalizedEvent(
        record_id=record_id,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=_first_str(payload.get("llm_call_id")),
        tool_call_id=_first_str(payload.get("tool_call_id")),
        tool_id=_first_str(payload.get("tool_id")),
        at=_first_str(payload.get("at")),
        payload=payload,
        hash=_first_str(raw.get("hash")),
        line_number=line_number,
    )


def _payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else raw


def _event_type(raw: Mapping[str, Any], payload: Mapping[str, Any], record_type: str) -> str:
    if record_type == "trace":
        return "trace.completed"
    return str(raw.get("eventType") or payload.get("type") or record_type or "unknown")


def _step_number(payload: Mapping[str, Any], nested_trace: Mapping[str, Any]) -> int | None:
    value = payload.get("step_number") if "step_number" in payload else nested_trace.get("step_number")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is not None:
            return str(value)
    return None


def _load_error(path: Path, message: str, *, line_number: int) -> Any:
    return make_loom_error("VALIDATION_FAILED", message, retryable=False, metadata={"path": str(path), "line": line_number})


__all__ = ["load_normalized_events", "normalize_record"]
