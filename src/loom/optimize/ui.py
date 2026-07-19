"""Observers for the committed optimization event stream."""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from loom.core import Result, err, make_loom_error, ok, thaw_json


class OptimizationObserver(Protocol):
    def emit(self, event: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class TextObserver:
    stream: TextIO = sys.stderr

    def emit(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "optimization.event")
        stage = event.get("stage", "unknown")
        status = event.get("status", "event")
        self.stream.write(f"[{status}] {event_type} {stage}\n")
        self.stream.flush()


@dataclass(slots=True)
class JsonObserver:
    stream: TextIO = sys.stdout

    def emit(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps(thaw_json(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        self.stream.flush()


class OptimizeTuiObserver:
    """Forward validated optimization envelopes to a live TUI collector."""

    def __init__(self, collector):
        self.collector = collector

    async def emit(self, event: dict[str, Any]) -> Any:
        value = self.collector.emit(event)
        return await value if inspect.isawaitable(value) else value


def create_observer(*, tui: bool, json_output: bool, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> Result:
    if tui and json_output:
        return err(make_loom_error("VALIDATION_FAILED", "--tui and --json are mutually exclusive", retryable=False))
    if json_output:
        return ok(JsonObserver(stdout))
    if not tui:
        return ok(TextObserver(stderr))
    return err(
        make_loom_error(
            "TUI_UNAVAILABLE",
            "Optimize TUI lifecycle is unavailable; run through loom optimize --tui with the tui extra installed",
            retryable=False,
        )
    )


__all__ = ["JsonObserver", "OptimizationObserver", "OptimizeTuiObserver", "TextObserver", "create_observer"]
