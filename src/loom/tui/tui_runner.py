"""Runners that stream Loom events to the TUI."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loom.core.models import Context, Result
from loom.runtime.engine import CancellationToken, LoopHandle
from loom.runtime.plugins import run_with_plugins
from loom.tui.plugin import TuiPlugin
from loom.tui.tui_collector import TuiEventCollector


async def run_with_tui(
    loop: LoopHandle,
    initial_context: Context,
    *,
    cancellation: CancellationToken | None = None,
    timeout_ms: int | None = None,
    max_steps: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Result:
    """Run a Loom loop with a real-time TUI visualization.

    This function:
    1. Creates a TUI plugin
    2. Runs the loop with the plugin's trace sink
    3. Keeps the TUI in the foreground after run completion until the user presses q
    4. Returns the run result after the TUI exits

    Args:
        loop: The LoopHandle to execute.
        initial_context: The starting context.
        cancellation: Optional cancellation token.
        timeout_ms: Optional timeout in milliseconds.
        max_steps: Optional maximum number of steps.
        metadata: Optional metadata to attach to the run.

    Returns:
        Result containing the RunResult or error after the user exits the TUI.
    """
    run_metadata = dict(metadata or {})
    run_metadata.setdefault("role", loop.definition.identity.role)
    run_metadata.setdefault("objective", loop.definition.goal.objective)
    return await run_with_plugins(
        loop,
        initial_context,
        plugins=(TuiPlugin(),),
        cancellation=cancellation,
        timeout_ms=timeout_ms,
        max_steps=max_steps,
        metadata=run_metadata,
    )


async def run_job_with_tui(
    job: Callable[[TuiEventCollector], Awaitable[Result]],
    *,
    role: str,
    goal: str,
    app_factory: Callable[[TuiEventCollector], Any] | None = None,
) -> Result:
    """Run an arbitrary async job while showing the shared Loom TUI.

    This is for event-producing jobs that are not runtime ``LoopHandle`` runs,
    such as trace evolution analysis. The job receives the same collector used
    by ``TuiPlugin``, so it can emit the same event protocol as normal loops.
    """
    collector = TuiEventCollector()
    app = _make_app(collector, app_factory)
    app.set_loop_info(role=role, goal=goal)
    app_task = asyncio.create_task(app.run_async())
    try:
        return await job(collector)
    finally:
        await collector.put_sentinel()
        await app_task


def _make_app(collector: TuiEventCollector, app_factory: Callable[[TuiEventCollector], Any] | None) -> Any:
    if app_factory is not None:
        return app_factory(collector)
    from loom.tui.tui_app import LoomTuiApp

    return LoomTuiApp(collector)


__all__ = ["run_job_with_tui", "run_with_tui"]
