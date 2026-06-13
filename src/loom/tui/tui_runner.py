"""Runner that executes a Loom loop and streams events to the TUI."""

from __future__ import annotations

import asyncio
from typing import Any

from loom.core.models import Context, Result
from loom.runtime.engine import CancellationToken, LoopHandle, run
from loom.tui.tui_app import LoomTuiApp
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
    1. Creates a TuiEventCollector to capture all trace events
    2. Starts the Textual TUI app
    3. Runs the loop in the background, streaming events to the TUI
    4. Returns the run result when complete

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
    collector = TuiEventCollector()

    # Build metadata with loop info for the TUI header
    run_metadata = dict(metadata or {})
    run_metadata.setdefault("role", loop.definition.identity.role)
    run_metadata.setdefault("objective", loop.definition.goal.objective)

    app = LoomTuiApp(collector)

    # Set loop info before the app starts
    app.set_loop_info(
        role=loop.definition.identity.role,
        goal=loop.definition.goal.objective,
    )

    result_holder: list[Result] = []

    async def _run_loop() -> None:
        """Run the loop and store the result."""
        try:
            result = await run(
                loop,
                initial_context,
                cancellation=cancellation,
                timeout_ms=timeout_ms,
                max_steps=max_steps,
                trace_sink=collector,
                metadata=run_metadata,
            )
            result_holder.append(result)
        except Exception as exc:
            from loom.core.models import err, make_loom_error

            result_holder.append(
                err(
                    make_loom_error(
                        "INTERNAL",
                        f"Loop execution failed: {exc}",
                        retryable=False,
                        cause={"exception": type(exc).__name__, "message": str(exc)},
                    )
                )
            )
        finally:
            await collector.put_sentinel()

    async def _run_app() -> None:
        """Run the TUI app."""
        await app.run_async()

    # Run both concurrently
    loop_task = asyncio.create_task(_run_loop())
    app_task = asyncio.create_task(_run_app())

    # Wait for the loop to finish
    await loop_task

    # Give the TUI a moment to process final events, then signal done
    await asyncio.sleep(0.2)

    # Wait for the app (user will quit with 'q' or it auto-exits)
    # We give the app a timeout so it doesn't hang forever
    try:
        await asyncio.wait_for(app_task, timeout=30.0)
    except TimeoutError:
        app.exit()
        await app_task

    # Return the result
    if result_holder:
        return result_holder[0]

    from loom.core.models import err, make_loom_error

    return err(make_loom_error("INTERNAL", "No result from loop execution", retryable=False))
