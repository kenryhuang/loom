"""Token usage helpers for evaluation artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from loom.evaluation.records import NormalizedEvent


def total_tokens_for_events(events: Iterable[NormalizedEvent]) -> int:
    """Return a non-duplicated token total for a set of trace events.

    LLM round completions are the primary source of truth. Trace, decision, and
    observation metadata can repeat the same aggregate usage, so metadata usage
    is only used when no completed LLM round has usage details.
    """

    event_items = tuple(events)
    llm_total = sum(_tokens_from_path(event.payload, ("response", "usage")) for event in event_items if event.event_type == "llm.completed")
    if llm_total:
        return llm_total
    return sum(
        _tokens_from_path(event.payload, ("metadata", "tokenUsage")) + _tokens_from_path(event.payload, ("metadata", "token_usage"))
        for event in event_items
        if event.event_type == "trace.completed"
    )


def _tokens_from_path(value: Any, path: tuple[str, ...]) -> int:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return 0
        current = current.get(key)
    return _tokens_from_mapping(current)


def _tokens_from_mapping(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    parsed = value.get("totalTokens") or value.get("total_tokens") or value.get("total") or 0
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return 0


__all__ = ["total_tokens_for_events"]
