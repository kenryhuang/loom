"""Episode graph construction for Loom evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from loom.evaluation.records import NormalizedEvent


@dataclass(frozen=True, slots=True)
class RunEpisode:
    id: str
    run_id: str
    loop_id: str | None
    step_ids: tuple[str, ...]
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StepGraphEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    status: str
    llm_round_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LlmRoundEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    llm_call_id: str
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    tool_call_id: str | None
    tool_id: str
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EpisodeGraph:
    events: tuple[NormalizedEvent, ...]
    runs: tuple[RunEpisode, ...]
    steps: tuple[StepGraphEpisode, ...]
    llm_rounds: tuple[LlmRoundEpisode, ...]
    tool_calls: tuple[ToolCallEpisode, ...]
    orphaned_events: tuple[NormalizedEvent, ...]
    event_hashes: tuple[str, ...]


def build_episode_graph(events: Iterable[NormalizedEvent]) -> EpisodeGraph:
    event_items = tuple(events)
    run_buckets: dict[str, dict[str, Any]] = {}
    step_buckets: dict[tuple[str, str, int], dict[str, Any]] = {}
    llm_buckets: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    tool_buckets: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    orphaned: list[NormalizedEvent] = []

    for event in event_items:
        if event.run_id:
            run_bucket = run_buckets.setdefault(event.run_id, {"events": [], "started": False, "completed": False})
            run_bucket["events"].append(event)
            if event.event_type == "run.started":
                run_bucket["started"] = True
            if event.event_type == "run.completed":
                run_bucket["completed"] = True

        step_key = _step_key(event)
        if step_key is not None:
            step_bucket = step_buckets.setdefault(step_key, {"events": [], "started": False, "completed": False, "trace_completed": False})
            step_bucket["events"].append(event)
            if event.event_type == "step.started":
                step_bucket["started"] = True
            if event.event_type == "step.completed":
                step_bucket["completed"] = True
            if event.event_type == "trace.completed":
                step_bucket["trace_completed"] = True
        elif event.event_type not in {"run.started", "run.completed"}:
            orphaned.append(event)

        if event.llm_call_id and step_key is not None:
            key = (*step_key, event.llm_call_id)
            bucket = llm_buckets.setdefault(key, {"events": [], "requested": False, "completed": False, "failed": False})
            bucket["events"].append(event)
            if event.event_type == "llm.requested":
                bucket["requested"] = True
            if event.event_type == "llm.completed":
                bucket["completed"] = True
            if event.event_type == "llm.failed":
                bucket["failed"] = True

        if event.event_type.startswith("tool.") and step_key is not None:
            call_identity = event.tool_call_id or event.tool_id or event.record_id
            key = (*step_key, call_identity)
            bucket = tool_buckets.setdefault(
            key,
            {
                "events": [],
                "started": False,
                "completed": False,
                "failed": False,
                "tool_id": event.tool_id or "unknown",
                "tool_call_id": event.tool_call_id,
            },
        )
            bucket["events"].append(event)
            if event.event_type == "tool.started":
                bucket["started"] = True
            if event.event_type == "tool.completed":
                bucket["completed"] = True
            if event.event_type == "tool.failed":
                bucket["failed"] = True

    llm_rounds = tuple(_llm_episode(key, bucket) for key, bucket in sorted(llm_buckets.items()))
    tool_calls = tuple(_tool_episode(key, bucket) for key, bucket in sorted(tool_buckets.items()))
    steps = tuple(_step_episode(key, bucket, llm_rounds, tool_calls) for key, bucket in sorted(step_buckets.items()))
    runs = tuple(_run_episode(run_id, bucket, steps) for run_id, bucket in sorted(run_buckets.items()))
    return EpisodeGraph(
        events=event_items,
        runs=runs,
        steps=steps,
        llm_rounds=llm_rounds,
        tool_calls=tool_calls,
        orphaned_events=tuple(orphaned),
        event_hashes=_hashes(event_items),
    )


def _step_key(event: NormalizedEvent) -> tuple[str, str, int] | None:
    if event.run_id is None or event.loop_id is None or event.trace_id is None or event.step_number is None:
        return None
    return (event.run_id, event.trace_id, event.step_number)


def _status(*, started: bool, completed: bool, failed: bool = False) -> str:
    if failed:
        return "failed"
    if started and completed:
        return "complete"
    return "partial"


def _llm_episode(key: tuple[str, str, int, str], bucket: dict[str, Any]) -> LlmRoundEpisode:
    run_id, trace_id, step_number, llm_call_id = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return LlmRoundEpisode(
        id=f"llm:{run_id}:{trace_id}:{step_number}:{llm_call_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=llm_call_id,
        status=_status(started=bool(bucket["requested"]), completed=bool(bucket["completed"]), failed=bool(bucket["failed"])),
        event_hashes=_hashes(events),
    )


def _tool_episode(key: tuple[str, str, int, str], bucket: dict[str, Any]) -> ToolCallEpisode:
    run_id, trace_id, step_number, call_identity = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return ToolCallEpisode(
        id=f"tool:{run_id}:{trace_id}:{step_number}:{call_identity}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        tool_call_id=bucket["tool_call_id"],
        tool_id=str(bucket["tool_id"]),
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]), failed=bool(bucket["failed"])),
        event_hashes=_hashes(events),
    )


def _step_episode(
    key: tuple[str, str, int],
    bucket: dict[str, Any],
    llm_rounds: tuple[LlmRoundEpisode, ...],
    tool_calls: tuple[ToolCallEpisode, ...],
) -> StepGraphEpisode:
    run_id, trace_id, step_number = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return StepGraphEpisode(
        id=f"step:{run_id}:{trace_id}:{step_number}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]) and bool(bucket["trace_completed"])),
        llm_round_ids=tuple(item.id for item in llm_rounds if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        tool_call_ids=tuple(item.id for item in tool_calls if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        event_hashes=_hashes(events),
    )


def _run_episode(run_id: str, bucket: dict[str, Any], steps: tuple[StepGraphEpisode, ...]) -> RunEpisode:
    events = tuple(bucket["events"])
    step_ids = tuple(step.id for step in steps if step.run_id == run_id)
    loop_id = next((event.loop_id for event in events if event.loop_id is not None), None)
    return RunEpisode(
        id=f"run:{run_id}",
        run_id=run_id,
        loop_id=loop_id,
        step_ids=step_ids,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"])),
        event_hashes=_hashes(events),
    )


def _hashes(events: tuple[NormalizedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.hash for event in events if event.hash is not None)


__all__ = ["EpisodeGraph", "LlmRoundEpisode", "RunEpisode", "StepGraphEpisode", "ToolCallEpisode", "build_episode_graph"]
