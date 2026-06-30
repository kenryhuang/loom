"""Lazy export helpers for the evolution package."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

_ANALYZE_EXPORTS = frozenset(
    {
        "AnalyzeConfig",
        "AnalyzeResult",
        "AnalyzeRunOptions",
        "analyze_trace",
        "main",
        "parse_args",
        "parse_run_options",
        "run_analyze_trace_with_tui",
    }
)


def lazy_analyze_export(namespace: dict[str, Any]) -> Callable[[str], Any]:
    def resolve(name: str) -> Any:
        if name in _ANALYZE_EXPORTS:
            value = getattr(import_module("loom.evolution.analyze"), name)
            namespace[name] = value
            return value
        raise AttributeError(f"module 'loom.evolution' has no attribute {name!r}")

    return resolve
