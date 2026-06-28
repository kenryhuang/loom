"""Trace episode construction for evolution analysis."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TraceRecord:
    record_type: str
    payload: Mapping[str, Any]
    raw: Mapping[str, Any]
    event_type: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    loop_id: str | None = None
    step_number: int | None = None
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class StepEpisode:
    run_id: str
    trace_id: str
    loop_id: str
    step_number: int
    events: tuple[Mapping[str, Any], ...]
    llm_requests: tuple[Mapping[str, Any], ...]
    llm_completions: tuple[Mapping[str, Any], ...]
    tool_events: tuple[Mapping[str, Any], ...]
    started_event: Mapping[str, Any] | None = None
    completed_event: Mapping[str, Any] | None = None
    completed_trace: Mapping[str, Any] | None = None
    complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "llm_requests", tuple(self.llm_requests))
        object.__setattr__(self, "llm_completions", tuple(self.llm_completions))
        object.__setattr__(self, "tool_events", tuple(self.tool_events))


def load_trace_records(path: str | Path) -> list[TraceRecord]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(_record_from_raw(json.loads(line)))
    return records


def build_step_episodes(records: Iterable[TraceRecord | Mapping[str, Any]]) -> list[StepEpisode]:
    episodes: list[StepEpisode] = []
    bucket: dict[str, Any] | None = None

    for item in records:
        record = item if isinstance(item, TraceRecord) else _record_from_raw(item)
        if record.record_type == "event" and record.event_type == "step.started":
            if bucket is not None:
                episodes.append(_episode_from_bucket(bucket))
            bucket = _new_bucket(record)
            continue

        if bucket is None or not _belongs_to_bucket(record, bucket):
            continue

        if record.record_type == "event":
            bucket["events"].append(record.payload)
            if record.event_type == "step.completed":
                bucket["completed_event"] = record.payload
        elif record.record_type == "trace":
            bucket["completed_trace"] = record.payload
            episodes.append(_episode_from_bucket(bucket))
            bucket = None

    if bucket is not None:
        episodes.append(_episode_from_bucket(bucket))
    return episodes


def _record_from_raw(raw: Mapping[str, Any]) -> TraceRecord:
    record_type = str(raw.get("type") or "")
    payload = _payload(raw)
    nested_trace = payload.get("trace")
    if not isinstance(nested_trace, Mapping):
        nested_trace = {}
    event_type = raw.get("eventType") if record_type == "event" else None
    if event_type is None and record_type == "event":
        event_type = payload.get("type")

    trace_id = raw.get("traceId")
    if trace_id is None and record_type == "trace":
        trace_id = raw.get("id")
    if trace_id is None:
        trace_id = payload.get("trace_id") or payload.get("id") or nested_trace.get("id")

    run_id = raw.get("runId") or payload.get("run_id") or nested_trace.get("run_id")
    loop_id = payload.get("loop_id") or nested_trace.get("loop_id")
    step_number = payload.get("step_number") if "step_number" in payload else nested_trace.get("step_number")
    return TraceRecord(
        record_type=record_type,
        payload=payload,
        raw=raw,
        event_type=event_type,
        trace_id=trace_id,
        run_id=run_id,
        loop_id=loop_id,
        step_number=step_number,
        hash=raw.get("hash"),
    )


def _episode_from_bucket(bucket: Mapping[str, Any]) -> StepEpisode:
    events = tuple(bucket["events"])
    completed_event = bucket.get("completed_event")
    completed_trace = bucket.get("completed_trace")
    return StepEpisode(
        run_id=bucket["run_id"],
        trace_id=bucket["trace_id"],
        loop_id=bucket["loop_id"],
        step_number=bucket["step_number"],
        events=events,
        llm_requests=tuple(event for event in events if event.get("type") == "llm.requested"),
        llm_completions=tuple(event for event in events if event.get("type") == "llm.completed"),
        tool_events=tuple(event for event in events if str(event.get("type") or "").startswith("tool.")),
        started_event=bucket.get("started_event"),
        completed_event=completed_event,
        completed_trace=completed_trace,
        complete=completed_event is not None and completed_trace is not None,
    )


def _new_bucket(record: TraceRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "trace_id": record.trace_id,
        "loop_id": record.loop_id,
        "step_number": record.step_number,
        "events": [record.payload],
        "started_event": record.payload,
        "completed_event": None,
        "completed_trace": None,
    }


def _belongs_to_bucket(record: TraceRecord, bucket: Mapping[str, Any]) -> bool:
    return (
        record.trace_id == bucket["trace_id"]
        and record.run_id == bucket["run_id"]
        and record.loop_id == bucket["loop_id"]
        and record.step_number == bucket["step_number"]
    )


def _payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else raw


__all__ = [
    "StepEpisode",
    "TraceRecord",
    "build_step_episodes",
    "load_trace_records",
]
