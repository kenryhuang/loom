"""Budgeted resolver for current-context tool affordances."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from loom.tools.contracts import AffordanceBudget, CatalogTool, ToolCatalog, ToolResolution, estimate_tool_schema_tokens


@dataclass(frozen=True, slots=True)
class ToolResolver:
    required_atomic_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_atomic_ids", tuple(self.required_atomic_ids))

    def resolve(
        self,
        catalog: ToolCatalog,
        budget: AffordanceBudget | None = None,
        *,
        ephemeral: Iterable[CatalogTool] | None = None,
    ) -> ToolResolution:
        budget = budget or AffordanceBudget()
        ephemeral_tools = tuple(ephemeral or ())

        required_atomic, optional_atomic = _partition_required(catalog.atomic, self.required_atomic_ids)
        ranked_composed = tuple(sorted(catalog.composed, key=_priority_score, reverse=True))
        composed = ranked_composed[: budget.max_composed_tools]
        ephemeral_limited = ephemeral_tools[: budget.max_ephemeral_tools]
        candidates = (*required_atomic, *optional_atomic, *composed, *ephemeral_limited)

        pruned: list[str] = _ids(ranked_composed[budget.max_composed_tools :]) + _ids(ephemeral_tools[budget.max_ephemeral_tools :])
        included: list[CatalogTool] = []
        total_tokens = 0
        over_budget = False

        for tool in candidates:
            tool_tokens = estimate_tool_schema_tokens(tool.ref)
            must_keep = tool.layer == "atomic" and tool.id in self.required_atomic_ids
            fits_count = len(included) < budget.max_tools
            fits_tokens = total_tokens + tool_tokens <= budget.max_tool_schema_tokens
            if must_keep or (fits_count and fits_tokens):
                included.append(tool)
                total_tokens += tool_tokens
                if must_keep and (not fits_count or not fits_tokens):
                    over_budget = True
            else:
                pruned.append(tool.id)

        return ToolResolution(
            tools=tuple(tool.ref for tool in included),
            included_ids=tuple(tool.id for tool in included),
            pruned_ids=tuple(dict.fromkeys(pruned)),
            token_estimate=total_tokens,
            over_budget=over_budget,
        )


def _partition_required(tools: tuple[CatalogTool, ...], required_ids: tuple[str, ...]) -> tuple[tuple[CatalogTool, ...], tuple[CatalogTool, ...]]:
    required = tuple(tool for tool in tools if tool.id in required_ids)
    optional = tuple(tool for tool in tools if tool.id not in required_ids)
    return required, optional


def _priority_score(tool: CatalogTool) -> float:
    lifecycle = tool.lifecycle
    return (
        lifecycle.success_rate
        + lifecycle.confidence_delta
        + lifecycle.usage_count * 0.01
        + lifecycle.distinct_context_shapes * 0.05
        + lifecycle.token_savings_estimate * 0.001
        - lifecycle.decay_score
    )


def _ids(tools: Iterable[CatalogTool]) -> list[str]:
    return [tool.id for tool in tools]
