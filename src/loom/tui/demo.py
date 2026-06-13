#!/usr/bin/env python3
"""Demo script: run a Loom loop with TUI visualization.

Usage:
    # Counter loop (no LLM, just demonstrates the loop mechanics):
    uv run python -m loom.tui.demo

    # LLM loop (requires .env with API key):
    LOOM_RUN_LIVE_LLM=1 uv run python -m loom.tui.demo
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure src is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from loom.examples.factories import (
    make_initial_counter_context,
    make_initial_llm_context,
    make_llm_loop_definition,
    make_minimal_counter_loop,
)
from loom.runtime.engine import create, create_runtime_registry, run
from loom.tui.tui_app import LoomTuiApp
from loom.tui.tui_collector import TuiEventCollector


async def demo_counter_loop() -> None:
    """Run the minimal counter loop with TUI."""
    collector = TuiEventCollector()

    loop_def = make_minimal_counter_loop()
    handle_result = create(
        loop_def,
        registry=create_runtime_registry(),
    )
    if not handle_result.ok:
        print(f"Failed to create loop: {handle_result.error}")
        return

    handle = handle_result.value
    context = make_initial_counter_context(max_steps=5)

    app = LoomTuiApp(collector)
    app.set_loop_info(role=loop_def.identity.role, goal=loop_def.goal.objective)

    result_holder: list = []

    async def _run() -> None:
        try:
            result = await run(
                handle,
                context,
                trace_sink=collector,
                metadata={"role": loop_def.identity.role, "objective": loop_def.goal.objective},
            )
            result_holder.append(result)
        finally:
            await collector.put_sentinel()

    loop_task = asyncio.create_task(_run())
    app_task = asyncio.create_task(app.run_async())

    await loop_task
    await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(app_task, timeout=60.0)
    except TimeoutError:
        app.exit()
        await app_task

    if result_holder and result_holder[0].ok:
        print(f"\nRun completed: {result_holder[0].value.metrics.steps} steps")
    elif result_holder:
        print(f"\nRun failed: {result_holder[0].error}")


async def demo_llm_loop() -> None:
    """Run the LLM loop with TUI (requires API key)."""
    collector = TuiEventCollector()

    from loom.llm.api import create_env_openai_provider

    provider_result = create_env_openai_provider()
    if not provider_result.ok:
        print(f"Failed to create LLM provider: {provider_result.error}")
        return

    provider = provider_result.value

    async def search_notes(input_value, _options=None):
        from loom.core.models import Observation, now_iso, ok

        return ok(
            Observation(
                "search-notes-observation",
                "search-notes",
                {
                    "input": input_value,
                    "matches": [
                        {
                            "title": "Loom architecture",
                            "summary": "Use context layers, Result values, and append-only state.",
                        }
                    ],
                },
                now_iso(),
            )
        )

    loop_def = make_llm_loop_definition({}, provider=provider)
    handle_result = create(
        loop_def,
        registry=create_runtime_registry(tools={"search-notes": search_notes}),
    )
    if not handle_result.ok:
        print(f"Failed to create loop: {handle_result.error}")
        return

    handle = handle_result.value
    context = make_initial_llm_context()

    app = LoomTuiApp(collector)
    app.set_loop_info(role=loop_def.identity.role, goal=loop_def.goal.objective)

    result_holder: list = []

    async def _run() -> None:
        try:
            result = await run(
                handle,
                context,
                max_steps=1,
                trace_sink=collector,
                metadata={"role": loop_def.identity.role, "objective": loop_def.goal.objective},
            )
            result_holder.append(result)
        finally:
            await collector.put_sentinel()

    loop_task = asyncio.create_task(_run())
    app_task = asyncio.create_task(app.run_async())

    await loop_task
    await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(app_task, timeout=120.0)
    except TimeoutError:
        app.exit()
        await app_task

    if result_holder and result_holder[0].ok:
        print(f"\nRun completed: {result_holder[0].value.metrics.steps} steps")
    elif result_holder:
        print(f"\nRun failed: {result_holder[0].error}")


def main() -> None:
    live_llm = os.environ.get("LOOM_RUN_LIVE_LLM", "").lower() in ("1", "true", "yes")
    if live_llm:
        print("Running LLM loop demo (requires API key in .env)...")
        asyncio.run(demo_llm_loop())
    else:
        print("Running counter loop demo...")
        asyncio.run(demo_counter_loop())


if __name__ == "__main__":
    main()
