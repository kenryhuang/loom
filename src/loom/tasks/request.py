"""Task request models for the generic Loom runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.llm.request_options import normalize_request_options


@dataclass(frozen=True, slots=True)
class TaskHarness:
    system_prompt_addendum: str = ""
    allowed_tools: tuple[str, ...] | None = None
    max_tool_calls_per_step: int | None = None
    max_history_steps: int = 5
    request_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.allowed_tools is not None:
            allowed_tools = tuple(self.allowed_tools)
            if any(not isinstance(tool, str) or not tool.strip() for tool in allowed_tools):
                raise ValueError("allowed_tools must contain non-empty tool IDs")
            if len(set(allowed_tools)) != len(allowed_tools):
                raise ValueError("allowed_tools must not contain duplicates")
            object.__setattr__(self, "allowed_tools", allowed_tools)
        if self.max_tool_calls_per_step is not None and self.max_tool_calls_per_step <= 0:
            raise ValueError("max_tool_calls_per_step must be positive")
        if self.max_history_steps <= 0:
            raise ValueError("max_history_steps must be positive")
        object.__setattr__(self, "request_options", normalize_request_options(self.request_options))


@dataclass(frozen=True, slots=True)
class TaskRequest:
    objective: str
    workspace: Path | None = None
    profile: str = "auto"
    constraints: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    risk_level: str = "auto"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskRunOptions:
    tui: bool = False
    stream: bool = False
    trace_path: Path | None = None
    max_steps: int | None = None
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if self.trace_path is not None:
            object.__setattr__(self, "trace_path", Path(self.trace_path))


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    run_result: Any
    output: str


__all__ = ["TaskHarness", "TaskRequest", "TaskRunOptions", "TaskRunResult"]
