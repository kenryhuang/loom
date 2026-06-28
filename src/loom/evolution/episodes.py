"""Trace episode construction for evolution analysis."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EpisodeIdentity = tuple[str, str, str, int]
EpisodePosition = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class TraceRecord:
    record_type: str
    payload: Mapping[str, Any]
    event_type: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    hash: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StepEpisode:
    run_id: str
    trace_id: str
    loop_id: str
    step_number: int
    started_event: Mapping[str, Any]
    llm_requests: tuple[Mapping[str, Any], ...]
    llm_completions: tuple[Mapping[str, Any], ...]
    tool_events: tuple[Mapping[str, Any], ...]
    action_events: tuple[Mapping[str, Any], ...]
    observation_events: tuple[Mapping[str, Any], ...]
    completed_trace: Mapping[str, Any] | None = None
    completed_event: Mapping[str, Any] | None = None
    event_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "llm_requests", tuple(self.llm_requests))
        object.__setattr__(self, "llm_completions", tuple(self.llm_completions))
        object.__setattr__(self, "tool_events", tuple(self.tool_events))
        object.__setattr__(self, "action_events", tuple(self.action_events))
        object.__setattr__(self, "observation_events", tuple(self.observation_events))
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))

    @property
    def complete(self) -> bool:
        return self.completed_event is not None and self.completed_trace is not None


def load_trace_records(path: str | Path) -> list[TraceRecord]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(_record_from_raw(json.loads(line)))
    return records


def build_step_episodes(records: Iterable[TraceRecord | Mapping[str, Any]]) -> list[StepEpisode]:
    buckets: dict[EpisodeIdentity, dict[str, Any]] = {}
    buckets_by_position: dict[EpisodePosition, list[dict[str, Any]]] = {}
    ordered_buckets: list[dict[str, Any]] = []

    for item in records:
        record = item if isinstance(item, TraceRecord) else _record_from_raw(item)
        identity = _episode_identity(record)
        position = _episode_position(record)
        if identity is None or position is None:
            continue

        if record.record_type == "event" and record.event_type == "step.started":
            if identity not in buckets:
                bucket = _new_bucket(record, identity)
                buckets[identity] = bucket
                buckets_by_position.setdefault(position, []).append(bucket)
                ordered_buckets.append(bucket)
            continue

        bucket = buckets.get(identity) or _bucket_by_position(buckets_by_position, position)
        if bucket is None:
            continue

        if record.record_type == "event":
            bucket["events"].append(record.payload)
            bucket["event_hashes"].extend(_hashes(record))
            if record.event_type == "step.completed":
                bucket["completed_event"] = record.payload
        elif record.record_type == "trace":
            bucket["completed_trace"] = record.payload
            bucket["event_hashes"].extend(_hashes(record))
            _alias_bucket(buckets, bucket, identity)

    return [_episode_from_bucket(bucket) for bucket in ordered_buckets]


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

    return TraceRecord(
        record_type=record_type,
        payload=payload,
        event_type=event_type,
        trace_id=trace_id,
        run_id=raw.get("runId") or payload.get("run_id") or nested_trace.get("run_id"),
        hash=raw.get("hash"),
        raw=raw,
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
        started_event=bucket["started_event"],
        llm_requests=tuple(event for event in events if event.get("type") == "llm.requested"),
        llm_completions=tuple(event for event in events if event.get("type") == "llm.completed"),
        tool_events=tuple(event for event in events if str(event.get("type") or "").startswith("tool.")),
        action_events=tuple(event for event in events if str(event.get("type") or "").startswith("action.")),
        observation_events=tuple(event for event in events if str(event.get("type") or "").startswith("observation.")),
        completed_trace=completed_trace,
        completed_event=completed_event,
        event_hashes=tuple(bucket["event_hashes"]),
    )


def _new_bucket(record: TraceRecord, identity: EpisodeIdentity) -> dict[str, Any]:
    run_id, trace_id, loop_id, step_number = identity
    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "loop_id": loop_id,
        "step_number": step_number,
        "events": [record.payload],
        "started_event": record.payload,
        "completed_event": None,
        "completed_trace": None,
        "event_hashes": _hashes(record),
    }


def _payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else raw


def _episode_identity(record: TraceRecord) -> EpisodeIdentity | None:
    run_id = record.run_id
    trace_id = record.trace_id
    loop_id = _loop_id(record)
    step_number = _step_number(record)
    if run_id is None or trace_id is None or loop_id is None or step_number is None:
        return None
    return (run_id, trace_id, loop_id, step_number)


def _episode_position(record: TraceRecord) -> EpisodePosition | None:
    run_id = record.run_id
    loop_id = _loop_id(record)
    step_number = _step_number(record)
    if run_id is None or loop_id is None or step_number is None:
        return None
    return (run_id, loop_id, step_number)


def _bucket_by_position(
    buckets_by_position: Mapping[EpisodePosition, list[dict[str, Any]]],
    position: EpisodePosition,
) -> dict[str, Any] | None:
    candidates = buckets_by_position.get(position, [])
    return candidates[0] if len(candidates) == 1 else None


def _alias_bucket(
    buckets: dict[EpisodeIdentity, dict[str, Any]],
    bucket: dict[str, Any],
    identity: EpisodeIdentity,
) -> None:
    buckets.setdefault(identity, bucket)
    bucket["trace_id"] = identity[1]


def _loop_id(record: TraceRecord) -> str | None:
    nested_trace = record.payload.get("trace")
    if not isinstance(nested_trace, Mapping):
        nested_trace = {}
    return record.payload.get("loop_id") or nested_trace.get("loop_id")


def _step_number(record: TraceRecord) -> int | None:
    nested_trace = record.payload.get("trace")
    if not isinstance(nested_trace, Mapping):
        nested_trace = {}
    return record.payload.get("step_number") if "step_number" in record.payload else nested_trace.get("step_number")


def _hashes(record: TraceRecord) -> list[str]:
    return [record.hash] if record.hash is not None else []


__all__ = [
    "StepEpisode",
    "TraceRecord",
    "build_step_episodes",
    "load_trace_records",
]
