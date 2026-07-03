"""Lazy export helpers for the evaluation package."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

_ANALYZE_EXPORTS = frozenset(
    {
        "EvaluationConfig",
        "EvaluationResult",
        "analyze_trace",
        "main",
        "parse_args",
    }
)


def lazy_analyze_export(namespace: dict[str, Any]) -> Callable[[str], Any]:
    def resolve(name: str) -> Any:
        if name in _ANALYZE_EXPORTS:
            value = getattr(import_module("loom.evaluation.analyze"), name)
            namespace[name] = value
            return value
        raise AttributeError(f"module 'loom.evaluation' has no attribute {name!r}")

    return resolve
