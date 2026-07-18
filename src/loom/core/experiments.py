"""Dependency-neutral experiment enums."""

from enum import StrEnum


class ExperimentPhase(StrEnum):
    DISCOVERY = "discovery"
    VALIDATION = "validation"
    HOLDOUT = "holdout"
    MONITORING = "monitoring"


class ExperimentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"


__all__ = ["ExperimentPhase", "ExperimentStatus"]
