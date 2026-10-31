"""Explicit workflow capability for applying trusted task feedback."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskFeedbackWorkflow(Protocol):
    def apply_task_feedback(self, feedback: dict) -> dict:
        """Idempotently arrange work, returning its feedback/step/revision binding."""
        ...
