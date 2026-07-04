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
