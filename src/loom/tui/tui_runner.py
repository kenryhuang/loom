"""Runner that executes a Loom loop and streams events to the TUI."""

from __future__ import annotations

from typing import Any

from loom.core.models import Context, Result
from loom.runtime.engine import CancellationToken, LoopHandle
from loom.runtime.plugins import run_with_plugins
from loom.tui.plugin import TuiPlugin


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
    3. Returns the run result when complete

    Args:
        loop: The LoopHandle to execute.
        initial_context: The starting context.
        cancellation: Optional cancellation token.
        timeout_ms: Optional timeout in milliseconds.
        max_steps: Optional maximum number of steps.
        metadata: Optional metadata to attach to the run.

    Returns:
        Result containing the RunResult or error.
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
