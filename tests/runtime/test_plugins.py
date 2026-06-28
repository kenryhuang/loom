from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from loom.core.models import Result, ok
from loom.examples.factories import make_initial_counter_context, make_minimal_counter_loop
from loom.runtime.engine import create, create_runtime_registry
from loom.runtime.plugins import RunPluginContext, run_with_plugins


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Mapping[str, Any]] = []

    async def emit(self, event: Mapping[str, Any]) -> Result:
        self.events.append(event)
        return ok(None)


class RecordingPlugin:
    def __init__(self) -> None:
        self.started: RunPluginContext | None = None
        self.stopped_with: Result | None = None
        self.sink = RecordingSink()

    async def start(self, context: RunPluginContext) -> Result:
        self.started = context
        return ok(None)

    def trace_sink(self) -> RecordingSink:
        return self.sink

    async def stop(self, result: Result | None) -> Result:
        self.stopped_with = result
        return ok(None)


@pytest.mark.asyncio
async def test_run_with_plugins_starts_plugin_streams_events_and_stops() -> None:
    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    plugin = RecordingPlugin()
    result = await run_with_plugins(
        handle_result.value,
        make_initial_counter_context(max_steps=1),
        plugins=(plugin,),
        max_steps=1,
        metadata={"source": "test"},
    )

    assert result.ok
    assert plugin.started is not None
    assert plugin.started.loop is handle_result.value
    assert plugin.started.metadata["source"] == "test"
    assert plugin.stopped_with is result
    assert [event["type"] for event in plugin.sink.events][:2] == ["run.started", "step.started"]
    assert plugin.sink.events[-1]["type"] == "run.completed"


class FailingStopPlugin(RecordingPlugin):
    async def stop(self, result: Result | None) -> Result:
        self.stopped_with = result
        from loom.core.models import err, make_loom_error

        return err(make_loom_error("INTERNAL", "plugin stop failed", retryable=False))


@pytest.mark.asyncio
async def test_run_with_plugins_is_fail_open_by_default_for_plugin_stop_errors() -> None:
    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    result = await run_with_plugins(
        handle_result.value,
        make_initial_counter_context(max_steps=1),
        plugins=(FailingStopPlugin(),),
        max_steps=1,
    )

    assert result.ok


@pytest.mark.asyncio
async def test_run_with_plugins_can_be_strict_for_plugin_stop_errors() -> None:
    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    result = await run_with_plugins(
        handle_result.value,
        make_initial_counter_context(max_steps=1),
        plugins=(FailingStopPlugin(),),
        max_steps=1,
        strict_plugins=True,
    )

    assert not result.ok
    assert result.error.message == "plugin stop failed"
