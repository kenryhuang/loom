"""Cooperative control outcomes, distinct from execution errors."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StepControl:
    kind: str
    reason: str = ""
    request_id: str | None = None

    def __post_init__(self):
        if self.kind not in {"continue", "completed", "paused", "waiting_input", "stopped"}:
            raise ValueError(f"Invalid step control: {self.kind}")
