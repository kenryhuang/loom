"""Task request models for the generic Loom runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


__all__ = ["TaskRequest", "TaskRunOptions", "TaskRunResult"]
