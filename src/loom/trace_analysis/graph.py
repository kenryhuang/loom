"""Episode graph construction for Loom trace analysis."""

from __future__ import annotations

from collections.abc import Callable, Iterable
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

RunKey = tuple[str, str | None]
StepKey = tuple[str, str, str, int]
LlmKey = tuple[str, str, str, int, str, int]
ToolKey = tuple[str, str, str, int, str, int]


def build_episode_graph(events: Iterable[NormalizedEvent]) -> EpisodeGraph:
    event_items = tuple(events)
    run_buckets: dict[RunKey, dict[str, Any]] = {}
    step_buckets: dict[StepKey, dict[str, Any]] = {}
    llm_buckets: dict[LlmKey, dict[str, Any]] = {}
    tool_buckets: dict[ToolKey, dict[str, Any]] = {}
    decisions: list[DecisionEpisode] = []
    observations: list[ObservationEpisode] = []
    orphaned: list[NormalizedEvent] = []

    for event_index, event in enumerate(event_items):
        if event.run_id:
            run_key = (event.run_id, event.loop_id)
            run_bucket = run_buckets.setdefault(run_key, {"events": [], "started": False, "completed": False})
            run_bucket["events"].append(event)
            if event.event_type == "run.started":
                run_bucket["started"] = True
                run_bucket["started_event"] = event
            if event.event_type == "run.completed":
                run_bucket["completed"] = True
                run_bucket["completed_event"] = event

        step_key = _step_key(event)
        if step_key is not None:
            step_bucket = step_buckets.setdefault(
                step_key,
                {"events": [], "started": False, "completed": False, "trace_completed": False},
            )
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

        if event.event_type.startswith("llm.") and event.llm_call_id and step_key is not None:
            key = _llm_key(event, step_key, llm_buckets)
            if key is None:
                orphaned.append(event)
            else:
                bucket = llm_buckets.setdefault(
                    key,
                    {"events": [], "requested": False, "completed": False, "failed": False, "stream_events": []},
                )
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
        elif event.event_type.startswith("llm.") and event.llm_call_id is None and step_key is not None:
            orphaned.append(event)

        if event.event_type.startswith("tool.") and step_key is not None:
            key = _tool_key(event, step_key, event_index, tool_buckets)
            if key is None:
                orphaned.append(event)
            else:
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
                if bucket["tool_id"] == "unknown" and event.tool_id is not None:
                    bucket["tool_id"] = event.tool_id
                if bucket["tool_call_id"] is None and event.tool_call_id is not None:
                    bucket["tool_call_id"] = event.tool_call_id
                if event.event_type == "tool.started":
                    bucket["started"] = True
                    bucket["started_event"] = event
                if event.event_type == "tool.completed":
                    bucket["completed"] = True
                    bucket["completed_event"] = event
                if event.event_type == "tool.failed":
                    bucket["failed"] = True
                    bucket["failed_event"] = event

    duplicate_llm_ids = _duplicate_base_ids(llm_buckets, _llm_base_id)
    duplicate_tool_ids = _duplicate_base_ids(tool_buckets, _tool_base_id)
    duplicate_step_ids = _duplicate_base_ids(step_buckets, _step_base_id)
    duplicate_run_ids = _duplicate_base_ids(run_buckets, _run_base_id)
    llm_rounds = tuple(
        _llm_episode(key, bucket, _llm_base_id(key) in duplicate_llm_ids)
        for key, bucket in llm_buckets.items()
    )
    tool_calls = tuple(
        _tool_episode(key, bucket, _tool_base_id(key) in duplicate_tool_ids)
        for key, bucket in tool_buckets.items()
    )
    runtime_steps = tuple(
        _step_episode(key, bucket, llm_rounds, tool_calls, _step_base_id(key) in duplicate_step_ids)
        for key, bucket in step_buckets.items()
    )
    runs = tuple(
        _run_episode(key, bucket, runtime_steps, _run_base_id(key) in duplicate_run_ids)
        for key, bucket in run_buckets.items()
    )
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


def _step_key(event: NormalizedEvent) -> StepKey | None:
    if event.run_id is None or event.loop_id is None or event.trace_id is None or event.step_number is None:
        return None
    return (event.run_id, event.loop_id, event.trace_id, event.step_number)


def _status(*, started: bool, completed: bool, failed: bool = False) -> str:
    if failed:
        return "failed"
    if started and completed:
        return "complete"
    return "partial"


def _llm_episode(key: LlmKey, bucket: dict[str, Any], duplicate_id: bool) -> LlmRoundEpisode:
    run_id, loop_id, trace_id, step_number, llm_call_id, occurrence = key
    events = tuple(bucket["events"])
    return LlmRoundEpisode(
        id=_identity(_llm_base_id(key), loop_id, duplicate_id, occurrence),
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


def _tool_episode(key: ToolKey, bucket: dict[str, Any], duplicate_id: bool) -> ToolCallEpisode:
    run_id, loop_id, trace_id, step_number, _call_identity, occurrence = key
    events = tuple(bucket["events"])
    return ToolCallEpisode(
        id=_identity(_tool_base_id(key), loop_id, duplicate_id, occurrence),
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
    key: StepKey,
    bucket: dict[str, Any],
    llm_rounds: tuple[LlmRoundEpisode, ...],
    tool_calls: tuple[ToolCallEpisode, ...],
    duplicate_id: bool,
) -> RuntimeStepEpisode:
    run_id, loop_id, trace_id, step_number = key
    events = tuple(bucket["events"])
    return RuntimeStepEpisode(
        id=_identity(_step_base_id(key), loop_id, duplicate_id),
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]) and bool(bucket["trace_completed"])),
        llm_round_ids=tuple(item.id for item in llm_rounds if _same_step(item, key)),
        tool_call_ids=tuple(item.id for item in tool_calls if _same_step(item, key)),
        event_hashes=_hashes(events),
        started_event=bucket.get("started_event"),
        completed_event=bucket.get("completed_event"),
        trace_completed_event=bucket.get("trace_completed_event"),
    )


def _run_episode(
    key: RunKey,
    bucket: dict[str, Any],
    runtime_steps: tuple[RuntimeStepEpisode, ...],
    duplicate_id: bool,
) -> RunEpisode:
    run_id, loop_id = key
    events = tuple(bucket["events"])
    step_ids = tuple(step.id for step in runtime_steps if step.run_id == run_id and step.loop_id == loop_id)
    return RunEpisode(
        id=_identity(_run_base_id(key), loop_id, duplicate_id),
        run_id=run_id,
        loop_id=loop_id,
        step_ids=step_ids,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"])),
        event_hashes=_hashes(events),
        started_event=bucket.get("started_event"),
        completed_event=bucket.get("completed_event"),
    )


def _decision_episode(step_key: StepKey, event: NormalizedEvent) -> DecisionEpisode:
    run_id, loop_id, trace_id, step_number = step_key
    return DecisionEpisode(
        id=f"decision:{run_id}:{loop_id}:{trace_id}:{step_number}:{event.record_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        event=event,
        event_hashes=_hashes((event,)),
    )


def _observation_episode(step_key: StepKey, event: NormalizedEvent) -> ObservationEpisode:
    run_id, loop_id, trace_id, step_number = step_key
    return ObservationEpisode(
        id=f"observation:{run_id}:{loop_id}:{trace_id}:{step_number}:{event.record_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        event=event,
        event_hashes=_hashes((event,)),
    )


def _hashes(events: tuple[NormalizedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.hash for event in events if event.hash is not None)


def _same_step(item: LlmRoundEpisode | ToolCallEpisode, key: StepKey) -> bool:
    run_id, loop_id, trace_id, step_number = key
    return (
        item.run_id == run_id
        and item.loop_id == loop_id
        and item.trace_id == trace_id
        and item.step_number == step_number
    )


def _llm_key(
    event: NormalizedEvent,
    step_key: StepKey,
    buckets: dict[LlmKey, dict[str, Any]],
) -> LlmKey | None:
    call_id = event.llm_call_id
    if call_id is None:
        return None
    matching = [(key, bucket) for key, bucket in buckets.items() if key[:5] == (*step_key, call_id)]
    active = [
        key
        for key, bucket in matching
        if not bucket["completed"] and not bucket["failed"]
    ]
    if event.event_type == "llm.requested":
        unrequested = [key for key, bucket in matching if key in active and not bucket["requested"]]
        if len(unrequested) == 1:
            return unrequested[0]
        return (*step_key, call_id, _next_occurrence(matching))
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        return None
    if matching and event.event_type in {"llm.completed", "llm.failed"}:
        duplicates = _duplicate_terminal_keys(event, matching)
        return duplicates[0] if len(duplicates) == 1 else None
    return (*step_key, call_id, _next_occurrence(matching))


def _tool_key(
    event: NormalizedEvent,
    step_key: StepKey,
    event_index: int,
    buckets: dict[ToolKey, dict[str, Any]],
) -> ToolKey | None:
    if event.tool_call_id is not None:
        matching = [(key, bucket) for key, bucket in buckets.items() if key[:5] == (*step_key, event.tool_call_id)]
        if event.event_type == "tool.started":
            return (*step_key, event.tool_call_id, _next_occurrence(matching))
        open_keys = [
            key
            for key, bucket in matching
            if bucket["started"] and not bucket["completed"] and not bucket["failed"]
        ]
        if len(open_keys) == 1:
            return open_keys[0]
        if len(open_keys) > 1:
            return None
        if matching and event.event_type in {"tool.completed", "tool.failed"}:
            duplicates = _duplicate_terminal_keys(event, matching)
            return duplicates[0] if len(duplicates) == 1 else None
        return (*step_key, event.tool_call_id, _next_occurrence(matching))
    if event.event_type in {"tool.completed", "tool.failed"}:
        candidates = [
            key
            for key, bucket in buckets.items()
            if key[:4] == step_key
            and bucket["tool_call_id"] is None
            and bucket["started"]
            and not bucket["completed"]
            and not bucket["failed"]
            and (event.tool_id is None or bucket["tool_id"] == event.tool_id)
        ]
        if len(candidates) == 1:
            return candidates[0]
    return (*step_key, f"anonymous:{event_index}", 1)


def _next_occurrence(matching: list[tuple[Any, dict[str, Any]]]) -> int:
    return max((int(key[-1]) for key, _bucket in matching), default=0) + 1


def _duplicate_terminal_keys(
    event: NormalizedEvent,
    matching: list[tuple[Any, dict[str, Any]]],
) -> list[Any]:
    terminal_name = "failed_event" if event.event_type.endswith(".failed") else "completed_event"
    return [
        key
        for key, bucket in matching
        if bucket.get(terminal_name) is not None and bucket[terminal_name].payload == event.payload
    ]


def _duplicate_base_ids(buckets: dict[Any, dict[str, Any]], make_id: Callable[[Any], str]) -> set[str]:
    loops: dict[str, set[str | None]] = {}
    for key in buckets:
        base_id = make_id(key)
        loops.setdefault(base_id, set()).add(key[1])
    return {base_id for base_id, loop_ids in loops.items() if len(loop_ids) > 1}


def _identity(base_id: str, loop_id: str | None, duplicate: bool, occurrence: int = 1) -> str:
    identity = f"{base_id}:loop:{loop_id or 'unknown'}" if duplicate else base_id
    if occurrence > 1:
        return f"{identity}:occurrence:{occurrence}"
    return identity


def _run_base_id(key: RunKey) -> str:
    run_id, _loop_id = key
    return f"run:{run_id}"


def _step_base_id(key: StepKey) -> str:
    run_id, _loop_id, trace_id, step_number = key
    return f"step:{run_id}:{trace_id}:{step_number}"


def _llm_base_id(key: LlmKey) -> str:
    run_id, _loop_id, trace_id, step_number, llm_call_id, _occurrence = key
    return f"llm:{run_id}:{trace_id}:{step_number}:{llm_call_id}"


def _tool_base_id(key: ToolKey) -> str:
    run_id, _loop_id, trace_id, step_number, call_identity, _occurrence = key
    return f"tool:{run_id}:{trace_id}:{step_number}:{call_identity}"


__all__ = ["build_episode_graph"]
