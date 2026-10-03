"""Allowlisted JSON checkpoint codec; never imports names supplied in data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from loom.core import models
from loom.llm.api import LlmMessage, LlmResponse, LlmToolCall, TokenUsage
from loom.runtime.control import StepControl

_NAMES = (
    "Constraint",
    "Capability",
    "IdentityLayer",
    "Budget",
    "SuccessCriterion",
    "GoalLayer",
    "Action",
    "Observation",
    "Decision",
    "PendingLoop",
    "StateLayer",
    "KnowledgeItem",
    "KnowledgeLayer",
    "ToolRef",
    "LoopRef",
    "ResourceRef",
    "AffordanceLayer",
    "Context",
    "TraceSnapshot",
    "Trace",
    "StepResult",
    "LoomError",
    "RunMetrics",
    "RunResult",
)
_TYPES = {name: getattr(models, name) for name in _NAMES}
_TYPES.update({cls.__name__: cls for cls in (LlmMessage, LlmResponse, LlmToolCall, TokenUsage, StepControl)})


def encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        if _TYPES.get(type(value).__name__) is not type(value):
            raise TypeError("Checkpoint type is not allowlisted")
        return {"$type": type(value).__name__, "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, Mapping):
        return {"$map": [[str(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    raise TypeError(f"Cannot checkpoint {type(value).__name__}")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$map"}:
        return {k: decode(v) for k, v in value["$map"]}
    if set(value) == {"$tuple"}:
        return tuple(decode(v) for v in value["$tuple"])
    if set(value) == {"$type", "fields"} and value["$type"] in _TYPES:
        cls = _TYPES[value["$type"]]
        raw = value["fields"]
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("Invalid checkpoint fields")
        try:
            return cls(**{k: decode(v) for k, v in raw.items()})
        except (TypeError, KeyError) as exc:
            raise ValueError("Invalid checkpoint object") from exc
    raise ValueError("Unsupported checkpoint encoding")


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value
