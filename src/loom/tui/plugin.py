"""TUI plugin for live Loom run visualization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loom.core.models import Result, ok
from loom.runtime.plugins import RunPluginContext
from loom.tui.tui_collector import TuiEventCollector


class TuiPlugin:
    def __init__(
        self,
        *,
        app_factory: Callable[[TuiEventCollector], Any] | None = None,
        auto_exit_timeout_seconds: float = 30.0,
    ) -> None:
        self._app_factory = app_factory
        # Retained for constructor compatibility; TUI now exits only by user action.
        self._auto_exit_timeout_seconds = auto_exit_timeout_seconds
        self.collector: TuiEventCollector | None = None
        self.app: Any | None = None
        self._app_task: asyncio.Task[Any] | None = None

    async def start(self, context: RunPluginContext) -> Result:
        self.collector = TuiEventCollector()
        self.app = self._make_app(self.collector)
        self.app.set_loop_info(
            role=context.loop.definition.identity.role,
            goal=context.loop.definition.goal.objective,
        )
        self._app_task = asyncio.create_task(self.app.run_async())
        return ok(None)

    def trace_sink(self) -> TuiEventCollector | None:
        return self.collector

    async def stop(self, _result: Result | None) -> Result:
        if self.collector is not None:
            await self.collector.put_sentinel()
        if self._app_task is not None:
            await self._app_task
        return ok(None)

    def _make_app(self, collector: TuiEventCollector) -> Any:
        if self._app_factory is not None:
            return self._app_factory(collector)
        from loom.tui.tui_app import LoomTuiApp

        return LoomTuiApp(collector)


__all__ = ["TuiPlugin"]
