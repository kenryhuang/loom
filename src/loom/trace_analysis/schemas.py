"""Shared dataclasses for trace analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    record_id: str
    event_type: str
    run_id: str | None
    loop_id: str | None
    trace_id: str | None
    step_number: int | None
    llm_call_id: str | None
    tool_call_id: str | None
    tool_id: str | None
    at: str | None
    payload: Mapping[str, Any]
    hash: str | None = None
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class TraceIngestResult:
    events: tuple[NormalizedEvent, ...]
    corrupt_records: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "corrupt_records", tuple(self.corrupt_records))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    event_hash: str | None
    event_type: str
    subject_id: str | None
    field_path: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    kind: str
    id: str


@dataclass(frozen=True, slots=True)
class RunEpisode:
    id: str
    run_id: str
    loop_id: str | None
    step_ids: tuple[str, ...]
    status: str
    event_hashes: tuple[str, ...]
    started_event: NormalizedEvent | None = None
    completed_event: NormalizedEvent | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_ids", tuple(self.step_ids))
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))


@dataclass(frozen=True, slots=True)
class RuntimeStepEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    status: str
    llm_round_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    event_hashes: tuple[str, ...]
    started_event: NormalizedEvent | None = None
    completed_event: NormalizedEvent | None = None
    trace_completed_event: NormalizedEvent | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "llm_round_ids", tuple(self.llm_round_ids))
        object.__setattr__(self, "tool_call_ids", tuple(self.tool_call_ids))
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))


StepGraphEpisode = RuntimeStepEpisode


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
    requested_event: NormalizedEvent | None = None
    stream_events: tuple[NormalizedEvent, ...] = ()
    completed_event: NormalizedEvent | None = None
    failed_event: NormalizedEvent | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))
        object.__setattr__(self, "stream_events", tuple(self.stream_events))


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
    started_event: NormalizedEvent | None = None
    completed_event: NormalizedEvent | None = None
    failed_event: NormalizedEvent | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))


@dataclass(frozen=True, slots=True)
class DecisionEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    event: NormalizedEvent
    event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))


@dataclass(frozen=True, slots=True)
class ObservationEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    event: NormalizedEvent
    event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))


@dataclass(frozen=True, slots=True)
class EpisodeGraph:
    events: tuple[NormalizedEvent, ...]
    runs: tuple[RunEpisode, ...]
    runtime_steps: tuple[RuntimeStepEpisode, ...]
    llm_rounds: tuple[LlmRoundEpisode, ...]
    tool_calls: tuple[ToolCallEpisode, ...]
    orphaned_events: tuple[NormalizedEvent, ...]
    event_hashes: tuple[str, ...]
    decisions: tuple[DecisionEpisode, ...] = ()
    observations: tuple[ObservationEpisode, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "runs", tuple(self.runs))
        object.__setattr__(self, "runtime_steps", tuple(self.runtime_steps))
        object.__setattr__(self, "llm_rounds", tuple(self.llm_rounds))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "orphaned_events", tuple(self.orphaned_events))
        object.__setattr__(self, "event_hashes", tuple(self.event_hashes))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "observations", tuple(self.observations))

    @property
    def steps(self) -> tuple[RuntimeStepEpisode, ...]:
        return self.runtime_steps
