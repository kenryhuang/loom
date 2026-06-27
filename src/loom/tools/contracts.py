"""Tool catalog contracts for bounded Loom tool governance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from loom.core.models import Result, ToolRef, err, make_loom_error, ok, thaw_json

ToolLayer = Literal["atomic", "composed", "ephemeral", "candidate"]


def _tuple(value: Iterable | None) -> tuple:
    return tuple(value or ())


@dataclass(frozen=True, slots=True)
class ToolLifecycle:
    layer: ToolLayer
    created_from_trace_ids: tuple[str, ...] = ()
    ttl_steps: int | None = None
    usage_count: int = 0
    distinct_context_shapes: int = 0
    success_rate: float = 0.0
    confidence_delta: float = 0.0
    token_savings_estimate: int = 0
    decay_score: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_from_trace_ids", _tuple(self.created_from_trace_ids))


@dataclass(frozen=True, slots=True)
class CatalogTool:
    ref: ToolRef
    lifecycle: ToolLifecycle
    atomic_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "atomic_dependencies", _tuple(self.atomic_dependencies))

    @property
    def id(self) -> str:
        return self.ref.id

    @property
    def layer(self) -> ToolLayer:
        return self.lifecycle.layer


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    atomic: tuple[CatalogTool, ...] = ()
    composed: tuple[CatalogTool, ...] = ()
    candidates: tuple[CatalogTool, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "atomic", _tuple(self.atomic))
        object.__setattr__(self, "composed", _tuple(self.composed))
        object.__setattr__(self, "candidates", _tuple(self.candidates))

    @property
    def all_tools(self) -> tuple[CatalogTool, ...]:
        return (*self.atomic, *self.composed, *self.candidates)

    def get(self, tool_id: str) -> Result:
        for tool in self.all_tools:
            if tool.id == tool_id:
                return ok(tool)
        return err(make_loom_error("VALIDATION_FAILED", "Tool not found", retryable=False, metadata={"tool_id": tool_id}))


@dataclass(frozen=True, slots=True)
class AffordanceBudget:
    max_tool_schema_tokens: int = 4000
    max_tools: int = 15
    max_composed_tools: int = 5
    max_ephemeral_tools: int = 3


@dataclass(frozen=True, slots=True)
class ToolResolution:
    tools: tuple[ToolRef, ...]
    included_ids: tuple[str, ...]
    pruned_ids: tuple[str, ...] = ()
    token_estimate: int = 0
    over_budget: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _tuple(self.tools))
        object.__setattr__(self, "included_ids", _tuple(self.included_ids))
        object.__setattr__(self, "pruned_ids", _tuple(self.pruned_ids))


def estimate_tool_schema_tokens(tool: ToolRef) -> int:
    payload = {
        "id": tool.id,
        "description": tool.description,
        "input_schema": thaw_json(tool.input_schema),
        "output_schema": thaw_json(tool.output_schema),
    }
    return max(1, len(json.dumps(payload, sort_keys=True, separators=(",", ":"))) // 4)
