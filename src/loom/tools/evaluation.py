"""Trace-driven helpers for tool evolution proposals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from loom.core.models import Trace


@dataclass(frozen=True, slots=True)
class ToolPattern:
    tool_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    distinct_context_shapes: int


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    reuse: bool = False
    compression: bool = False
    quality: bool = False
    stability: bool = False
    auditability: bool = False

    @property
    def score(self) -> int:
        return sum((self.reuse, self.compression, self.quality, self.stability, self.auditability))


def detect_tool_patterns(traces: tuple[Trace, ...], *, min_distinct_contexts: int = 3) -> tuple[ToolPattern, ...]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = defaultdict(lambda: {"trace_ids": [], "contexts": set()})
    for trace in traces:
        tool_ids = tuple(sorted({action.target for action in trace.actions if action.kind == "tool" and action.target}))
        if len(tool_ids) < 2:
            continue
        grouped[tool_ids]["trace_ids"].append(trace.id)
        grouped[tool_ids]["contexts"].add(trace.input_context_id)

    patterns = []
    for tool_ids, data in grouped.items():
        contexts = data["contexts"]
        trace_ids = data["trace_ids"]
        if len(contexts) >= min_distinct_contexts:
            patterns.append(ToolPattern(tool_ids, tuple(trace_ids), len(contexts)))
    return tuple(sorted(patterns, key=lambda pattern: (-pattern.distinct_context_shapes, pattern.tool_ids)))


def should_promote(evidence: PromotionEvidence, *, minimum_categories: int = 2) -> bool:
    return evidence.score >= minimum_categories
