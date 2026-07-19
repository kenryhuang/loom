"""Observers for the committed optimization event stream."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from loom.core import Result, err, make_loom_error, ok


class OptimizationObserver(Protocol):
    def emit(self, event: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class TextObserver:
    stream: TextIO = sys.stderr

    def emit(self, event: dict[str, Any]) -> None:
        stage = event.get("stage", "unknown")
        status = event.get("status", "event")
        self.stream.write(f"[{status}] {stage}\n")
        self.stream.flush()


@dataclass(slots=True)
class JsonObserver:
    stream: TextIO = sys.stdout

    def emit(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        self.stream.flush()


class TuiObserver:
    """Small Rich-backed observer over the same committed event mappings."""

    def __init__(self, console):
        self.console = console

    def emit(self, event: dict[str, Any]) -> None:
        self.console.print(f"[cyan]{event.get('stage', 'unknown')}[/cyan] {event.get('status', 'event')}")


def create_observer(*, tui: bool, json_output: bool, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> Result:
    if tui and json_output:
        return err(make_loom_error("VALIDATION_FAILED", "--tui and --json are mutually exclusive", retryable=False))
    if json_output:
        return ok(JsonObserver(stdout))
    if not tui:
        return ok(TextObserver(stderr))
    try:
        from rich.console import Console
    except ImportError:
        return err(
            make_loom_error(
                "TUI_UNAVAILABLE",
                "TUI dependencies are unavailable; install Loom with the tui extra",
                retryable=False,
            )
        )
    return ok(TuiObserver(Console(file=stderr)))


__all__ = ["JsonObserver", "OptimizationObserver", "TextObserver", "TuiObserver", "create_observer"]
