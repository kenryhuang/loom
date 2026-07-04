"""Episode graph construction for Loom trace analysis."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from loom.trace_analysis.schemas import (
    DecisionEpisode,
    EpisodeGraph,
    LlmRoundEpisode,
    NormalizedEvent,
    ObservationEpisode,
    RunEpisode,
    RuntimeStepEpisode,
    ToolCallEpisode,
)


def build_episode_graph(events: Iterable[NormalizedEvent]) -> EpisodeGraph:
    event_items = tuple(events)
    run_buckets: dict[str, dict[str, Any]] = {}
    step_buckets: dict[tuple[str, str, int], dict[str, Any]] = {}
    llm_buckets: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    tool_buckets: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    decisions: list[DecisionEpisode] = []
    observations: list[ObservationEpisode] = []
    orphaned: list[NormalizedEvent] = []

    for event in event_items:
        if event.run_id:
            run_bucket = run_buckets.setdefault(event.run_id, {"events": [], "started": False, "completed": False})
            run_bucket["events"].append(event)
            if event.event_type == "run.started":
                run_bucket["started"] = True
                run_bucket["started_event"] = event
            if event.event_type == "run.completed":
                run_bucket["completed"] = True
                run_bucket["completed_event"] = event

        step_key = _step_key(event)
        if step_key is not None:
            step_bucket = step_buckets.setdefault(step_key, {"events": [], "started": False, "completed": False, "trace_completed": False})
            step_bucket["events"].append(event)
            if event.event_type == "step.started":
                step_bucket["started"] = True
                step_bucket["started_event"] = event
            if event.event_type == "step.completed":
                step_bucket["completed"] = True
                step_bucket["completed_event"] = event
            if event.event_type == "trace.completed":
                step_bucket["trace_completed"] = True
                step_bucket["trace_completed_event"] = event
            if event.event_type == "decision.recorded":
                decisions.append(_decision_episode(step_key, event))
            if event.event_type == "observation.recorded":
                observations.append(_observation_episode(step_key, event))
        elif event.event_type not in {"run.started", "run.completed"}:
            orphaned.append(event)

        if event.llm_call_id and step_key is not None:
            key = (*step_key, event.llm_call_id)
            bucket = llm_buckets.setdefault(key, {"events": [], "requested": False, "completed": False, "failed": False, "stream_events": []})
            bucket["events"].append(event)
            if event.event_type == "llm.requested":
                bucket["requested"] = True
                bucket["requested_event"] = event
            if event.event_type in {"llm.stream.started", "llm.stream.completed"} or event.event_type.endswith(".delta"):
                bucket["stream_events"].append(event)
            if event.event_type == "llm.completed":
                bucket["completed"] = True
                bucket["completed_event"] = event
            if event.event_type == "llm.failed":
                bucket["failed"] = True
                bucket["failed_event"] = event

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
                bucket["started_event"] = event
            if event.event_type == "tool.completed":
                bucket["completed"] = True
                bucket["completed_event"] = event
            if event.event_type == "tool.failed":
                bucket["failed"] = True
                bucket["failed_event"] = event

    llm_rounds = tuple(_llm_episode(key, bucket) for key, bucket in sorted(llm_buckets.items()))
    tool_calls = tuple(_tool_episode(key, bucket) for key, bucket in sorted(tool_buckets.items()))
    runtime_steps = tuple(_step_episode(key, bucket, llm_rounds, tool_calls) for key, bucket in sorted(step_buckets.items()))
    runs = tuple(_run_episode(run_id, bucket, runtime_steps) for run_id, bucket in sorted(run_buckets.items()))
    return EpisodeGraph(
        events=event_items,
        runs=runs,
        runtime_steps=runtime_steps,
        llm_rounds=llm_rounds,
        tool_calls=tool_calls,
        orphaned_events=tuple(orphaned),
        event_hashes=_hashes(event_items),
        decisions=tuple(decisions),
        observations=tuple(observations),
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
        requested_event=bucket.get("requested_event"),
        stream_events=tuple(bucket["stream_events"]),
        completed_event=bucket.get("completed_event"),
        failed_event=bucket.get("failed_event"),
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
        started_event=bucket.get("started_event"),
        completed_event=bucket.get("completed_event"),
        failed_event=bucket.get("failed_event"),
    )


def _step_episode(
    key: tuple[str, str, int],
    bucket: dict[str, Any],
    llm_rounds: tuple[LlmRoundEpisode, ...],
    tool_calls: tuple[ToolCallEpisode, ...],
) -> RuntimeStepEpisode:
    run_id, trace_id, step_number = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return RuntimeStepEpisode(
        id=f"step:{run_id}:{trace_id}:{step_number}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]) and bool(bucket["trace_completed"])),
        llm_round_ids=tuple(item.id for item in llm_rounds if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        tool_call_ids=tuple(item.id for item in tool_calls if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        event_hashes=_hashes(events),
        started_event=bucket.get("started_event"),
        completed_event=bucket.get("completed_event"),
        trace_completed_event=bucket.get("trace_completed_event"),
    )


def _run_episode(run_id: str, bucket: dict[str, Any], runtime_steps: tuple[RuntimeStepEpisode, ...]) -> RunEpisode:
    events = tuple(bucket["events"])
    step_ids = tuple(step.id for step in runtime_steps if step.run_id == run_id)
    loop_id = next((event.loop_id for event in events if event.loop_id is not None), None)
    return RunEpisode(
        id=f"run:{run_id}",
        run_id=run_id,
        loop_id=loop_id,
        step_ids=step_ids,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"])),
        event_hashes=_hashes(events),
        started_event=bucket.get("started_event"),
        completed_event=bucket.get("completed_event"),
    )


def _decision_episode(step_key: tuple[str, str, int], event: NormalizedEvent) -> DecisionEpisode:
    run_id, trace_id, step_number = step_key
    loop_id = event.loop_id or ""
    return DecisionEpisode(
        id=f"decision:{run_id}:{trace_id}:{step_number}:{event.record_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        event=event,
        event_hashes=_hashes((event,)),
    )


def _observation_episode(step_key: tuple[str, str, int], event: NormalizedEvent) -> ObservationEpisode:
    run_id, trace_id, step_number = step_key
    loop_id = event.loop_id or ""
    return ObservationEpisode(
        id=f"observation:{run_id}:{trace_id}:{step_number}:{event.record_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        event=event,
        event_hashes=_hashes((event,)),
    )


def _hashes(events: tuple[NormalizedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.hash for event in events if event.hash is not None)


__all__ = ["build_episode_graph"]
