"""Plugin orchestration for Loom runtime runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from loom.core.models import Context, LoopHandle, Result, err, make_loom_error, ok
from loom.observability.traces import CompositeTraceSink
from loom.runtime.engine import CancellationToken, run


@dataclass(frozen=True, slots=True)
class RunPluginContext:
    loop: LoopHandle
    initial_context: Context
    metadata: Mapping[str, Any]


class LoopPlugin(Protocol):
    async def start(self, context: RunPluginContext) -> Result: ...

    def trace_sink(self) -> Any | None: ...

    async def stop(self, result: Result | None) -> Result: ...


async def run_with_plugins(
    loop: LoopHandle,
    initial_context: Context,
    *,
    plugins: tuple[LoopPlugin, ...] = (),
    cancellation: CancellationToken | None = None,
    timeout_ms: int | None = None,
    max_steps: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    trace_sink: Any | None = None,
    strict_plugins: bool = False,
) -> Result:
    """Run a loop with optional observer plugins.

    Plugins are fail-open by default: plugin start/stop failures do not change
    the loop result unless ``strict_plugins`` is enabled.
    """
    run_metadata = dict(metadata or {})
    context = RunPluginContext(loop=loop, initial_context=initial_context, metadata=run_metadata)
    started_plugins: list[LoopPlugin] = []
    plugin_sinks: list[Any] = []

    for plugin in plugins:
        started = await _safe_plugin_start(plugin, context)
        if not started.ok:
            if strict_plugins:
                return started
            continue
        started_plugins.append(plugin)
        sink = plugin.trace_sink()
        if sink is not None:
            plugin_sinks.append(sink)

    sinks = tuple(item for item in (*plugin_sinks, trace_sink) if item is not None)
    observer_sink = CompositeTraceSink(sinks) if len(sinks) > 1 else sinks[0] if sinks else None

    run_result: Result | None = None
    stop_error: Result | None = None
    try:
        run_result = await run(
            loop,
            initial_context,
            cancellation=cancellation,
            timeout_ms=timeout_ms,
            max_steps=max_steps,
            trace_sink=observer_sink,
            metadata=run_metadata,
        )
    finally:
        for plugin in reversed(started_plugins):
            stopped = await _safe_plugin_stop(plugin, run_result)
            if strict_plugins and not stopped.ok and stop_error is None:
                stop_error = stopped

    if stop_error is not None and (run_result is None or run_result.ok):
        return stop_error
    if run_result is None:
        return err(make_loom_error("INTERNAL", "No result from plugin run", retryable=False))
    return run_result


async def _safe_plugin_start(plugin: LoopPlugin, context: RunPluginContext) -> Result:
    try:
        return await plugin.start(context)
    except BaseException as exc:
        return err(
            make_loom_error(
                "INTERNAL",
                f"Plugin start failed: {exc}",
                retryable=False,
                cause={"exception": type(exc).__name__, "message": str(exc)},
            )
        )


async def _safe_plugin_stop(plugin: LoopPlugin, result: Result | None) -> Result:
    try:
        return await plugin.stop(result)
    except BaseException as exc:
        return err(
            make_loom_error(
                "INTERNAL",
                f"Plugin stop failed: {exc}",
                retryable=False,
                cause={"exception": type(exc).__name__, "message": str(exc)},
            )
        )


__all__ = ["LoopPlugin", "RunPluginContext", "run_with_plugins"]
