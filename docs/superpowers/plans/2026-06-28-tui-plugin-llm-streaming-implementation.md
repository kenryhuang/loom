# TUI Plugin LLM Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a runtime plugin layer, make TUI a plugin, stream LLM/provider events into trace sinks, and add an LLM-judged real project smoke mode.

**Architecture:** Keep `loom.runtime.engine.run()` UI-agnostic and add `loom.runtime.plugins.run_with_plugins()` as orchestration above it. Add optional streaming to the existing LLM step function without removing `provider.chat()`. Move TUI lifecycle ownership into `loom.tui.plugin.TuiPlugin`, and wire `real_project_smoke --llm --tui` through the same plugin and streaming path.

**Tech Stack:** Python 3.11, dataclasses, async/await, existing `Result` contracts, existing `TraceSink`/`CompositeTraceSink`, Textual/Rich TUI, pytest/pytest-asyncio, ruff.

---

## File Map

- Create `src/loom/runtime/plugins.py`: runtime plugin protocol, plugin context, and `run_with_plugins()`.
- Modify `src/loom/runtime/__init__.py`: export plugin runner types.
- Create `tests/runtime/test_plugins.py`: plugin lifecycle and fail-open/strict tests.
- Create `src/loom/tui/plugin.py`: `TuiPlugin` implementation.
- Modify `src/loom/tui/tui_runner.py`: make `run_with_tui()` delegate to `run_with_plugins()`.
- Modify `src/loom/tui/__init__.py`: export `TuiPlugin`.
- Create `tests/unit/test_tui_plugin.py`: TUI plugin lifecycle with injected fake app.
- Modify `src/loom/llm/api.py`: add `LlmStreamEvent`, optional streaming in `create_llm_step_function()`, and OpenAI-compatible SSE parsing.
- Modify `src/loom/llm/__init__.py`: export `LlmStreamEvent`.
- Modify `tests/llm/test_llm.py`: add streaming LLM step and OpenAI provider streaming tests.
- Modify `src/loom/tui/tui_collector.py`: track stream-call duration and normalize stream IDs.
- Modify `src/loom/tui/tui_app.py`: render stream delta events.
- Modify `tests/unit/test_tui_app.py`: verify stream event formatting.
- Modify `src/loom/examples/real_project_smoke.py`: add LLM mode, provider wiring, `--llm`, `--tui`, and `--stream`.
- Modify `tests/integration/test_real_project_smoke.py`: add fake-provider LLM mode tests.

---

### Task 1: Runtime Plugin Runner

**Files:**
- Create: `src/loom/runtime/plugins.py`
- Modify: `src/loom/runtime/__init__.py`
- Test: `tests/runtime/test_plugins.py`

- [ ] **Step 1: Write failing plugin lifecycle tests**

Add `tests/runtime/test_plugins.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from loom.core.models import Context, Result, StepResult, Trace, as_step_number, freeze_context, new_context_id, new_loop_id, new_loop_version, ok
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
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/runtime/test_plugins.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'loom.runtime.plugins'`.

- [ ] **Step 3: Implement plugin runner**

Create `src/loom/runtime/plugins.py`:

```python
"""Plugin orchestration for Loom runtime runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from loom.core.models import Context, LoopHandle, Result, ok
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
    run_metadata = dict(metadata or {})
    context = RunPluginContext(loop=loop, initial_context=initial_context, metadata=run_metadata)
    started_plugins: list[LoopPlugin] = []
    plugin_sinks: list[Any] = []
    run_result: Result | None = None

    for plugin in plugins:
        started = await plugin.start(context)
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
        return run_result
    finally:
        final_result = run_result
        for plugin in reversed(started_plugins):
            stopped = await plugin.stop(final_result)
            if strict_plugins and not stopped.ok and (final_result is None or final_result.ok):
                run_result = stopped
```

Then fix the strict branch so the function can return strict stop errors:

```python
    stop_error: Result | None = None
    try:
        run_result = await run(...)
    finally:
        for plugin in reversed(started_plugins):
            stopped = await plugin.stop(run_result)
            if strict_plugins and not stopped.ok and stop_error is None:
                stop_error = stopped
    if stop_error is not None and (run_result is None or run_result.ok):
        return stop_error
    return run_result
```

Modify `src/loom/runtime/__init__.py` to export:

```python
from loom.runtime.plugins import LoopPlugin, RunPluginContext, run_with_plugins
```

and add the names to `__all__`.

- [ ] **Step 4: Run plugin tests to verify GREEN**

Run:

```bash
uv run pytest tests/runtime/test_plugins.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/loom/runtime/plugins.py src/loom/runtime/__init__.py tests/runtime/test_plugins.py
git commit -m "feat: add runtime plugin runner"
```

---

### Task 2: TUI Plugin Wrapper

**Files:**
- Create: `src/loom/tui/plugin.py`
- Modify: `src/loom/tui/tui_runner.py`
- Modify: `src/loom/tui/__init__.py`
- Test: `tests/unit/test_tui_plugin.py`

- [ ] **Step 1: Write failing TUI plugin tests**

Add `tests/unit/test_tui_plugin.py`:

```python
from __future__ import annotations

import asyncio

import pytest

from loom.examples.factories import make_initial_counter_context, make_minimal_counter_loop
from loom.runtime.engine import create, create_runtime_registry
from loom.runtime.plugins import RunPluginContext, run_with_plugins
from loom.tui.plugin import TuiPlugin
from loom.tui.tui_collector import TuiEventCollector


class ImmediateApp:
    def __init__(self, collector: TuiEventCollector) -> None:
        self.collector = collector
        self.loop_info: tuple[str, str] | None = None
        self.exited = False
        self.ran = False

    def set_loop_info(self, *, role: str, goal: str) -> None:
        self.loop_info = (role, goal)

    async def run_async(self) -> None:
        self.ran = True

    def exit(self) -> None:
        self.exited = True


@pytest.mark.asyncio
async def test_tui_plugin_exposes_collector_sink_and_sends_done_sentinel() -> None:
    app_holder: list[ImmediateApp] = []

    def app_factory(collector: TuiEventCollector) -> ImmediateApp:
        app = ImmediateApp(collector)
        app_holder.append(app)
        return app

    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    plugin = TuiPlugin(app_factory=app_factory, auto_exit_timeout_seconds=0.01)
    result = await run_with_plugins(
        handle_result.value,
        make_initial_counter_context(max_steps=1),
        plugins=(plugin,),
        max_steps=1,
    )

    assert result.ok
    assert plugin.collector is not None
    assert plugin.collector.event_count >= 1
    assert app_holder[0].loop_info == (loop_def.identity.role, loop_def.goal.objective)
    assert app_holder[0].ran is True
    queued = list(plugin.collector.queue._queue)
    assert queued[-1].event_type == "_tui_done"
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/unit/test_tui_plugin.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'loom.tui.plugin'`.

- [ ] **Step 3: Implement `TuiPlugin`**

Create `src/loom/tui/plugin.py`:

```python
"""TUI plugin for live Loom run visualization."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loom.core.models import Result, ok
from loom.runtime.plugins import RunPluginContext
from loom.tui.tui_app import LoomTuiApp
from loom.tui.tui_collector import TuiEventCollector


class TuiPlugin:
    def __init__(
        self,
        *,
        app_factory: Callable[[TuiEventCollector], Any] | None = None,
        auto_exit_timeout_seconds: float = 30.0,
    ) -> None:
        self._app_factory = app_factory or LoomTuiApp
        self._auto_exit_timeout_seconds = auto_exit_timeout_seconds
        self.collector: TuiEventCollector | None = None
        self.app: Any | None = None
        self._app_task: asyncio.Task[Any] | None = None

    async def start(self, context: RunPluginContext) -> Result:
        self.collector = TuiEventCollector()
        self.app = self._app_factory(self.collector)
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
            try:
                await asyncio.wait_for(self._app_task, timeout=self._auto_exit_timeout_seconds)
            except TimeoutError:
                if self.app is not None:
                    self.app.exit()
                await self._app_task
        return ok(None)
```

Modify `src/loom/tui/tui_runner.py` so `run_with_tui()` delegates:

```python
from loom.runtime.plugins import run_with_plugins
from loom.tui.plugin import TuiPlugin

async def run_with_tui(...):
    return await run_with_plugins(
        loop,
        initial_context,
        plugins=(TuiPlugin(),),
        cancellation=cancellation,
        timeout_ms=timeout_ms,
        max_steps=max_steps,
        metadata=metadata,
    )
```

Modify `src/loom/tui/__init__.py`:

```python
from loom.tui.plugin import TuiPlugin
```

- [ ] **Step 4: Run TUI plugin tests to verify GREEN**

Run:

```bash
uv run pytest tests/unit/test_tui_plugin.py tests/unit/test_tui_app.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/loom/tui/plugin.py src/loom/tui/tui_runner.py src/loom/tui/__init__.py tests/unit/test_tui_plugin.py
git commit -m "feat: expose tui as runtime plugin"
```

---

### Task 3: LLM Streaming Contract and Step Events

**Files:**
- Modify: `src/loom/llm/api.py`
- Modify: `src/loom/llm/__init__.py`
- Test: `tests/llm/test_llm.py`

- [ ] **Step 1: Write failing streaming step test**

Add to `tests/llm/test_llm.py`:

```python
from loom.llm import LlmStreamEvent
```

Then add:

```python
def test_llm_step_streaming_provider_emits_delta_events_and_executes_tool_calls():
    async def scenario():
        events = []
        provider = FakeStreamingProvider(
            [
                [
                    LlmStreamEvent(kind="tool_call.started", tool_call_id="call_1", tool_name="search"),
                    LlmStreamEvent(kind="tool_call.arguments.delta", tool_call_id="call_1", tool_arguments_delta='{"query":"loom"}'),
                    LlmStreamEvent(
                        kind="tool_call.completed",
                        tool_call_id="call_1",
                        tool_name="search",
                        tool_arguments_delta='{"query":"loom"}',
                    ),
                    LlmStreamEvent(
                        kind="completed",
                        response=LlmResponse(
                            content=None,
                            tool_calls=(LlmToolCall("call_1", "search", '{"query":"loom"}'),),
                            finish_reason="tool_calls",
                        ),
                    ),
                ],
                [
                    LlmStreamEvent(kind="reasoning.delta", reasoning_delta="Evidence is enough."),
                    LlmStreamEvent(kind="content.delta", content_delta='{"reasoning":"Tool result is enough",'),
                    LlmStreamEvent(kind="content.delta", content_delta='"action":{"kind":"tool","target":"search","description":"Use the search result","input":{"query":"loom"}},"alternatives":[],"confidence":0.8}'),
                    LlmStreamEvent(
                        kind="completed",
                        response=LlmResponse(
                            content='{"reasoning":"Tool result is enough","action":{"kind":"tool","target":"search","description":"Use the search result","input":{"query":"loom"}},"alternatives":[],"confidence":0.8}',
                            usage=TokenUsage(12, 6, 18),
                        ),
                    ),
                ],
            ]
        )
        tool_calls = []

        async def call_tool(name, input_value, **options):
            tool_calls.append((name, input_value, options))
            return ok(Observation("search-obs", "search", {"result": "found"}, NOW))

        result = await create_llm_step_function(provider, stream=True)(make_context(), make_runtime(call_tool=call_tool, events=events))

        assert result.ok
        assert tool_calls[0][0] == "search"
        event_types = [event["type"] for event in events]
        assert "llm.stream.started" in event_types
        assert "llm.tool_call.started" in event_types
        assert "llm.tool_call.arguments.delta" in event_types
        assert "llm.tool_call.completed" in event_types
        assert "llm.reasoning.delta" in event_types
        assert "llm.content.delta" in event_types
        assert event_types.count("llm.stream.completed") == 2
        assert events[event_types.index("llm.content.delta")]["delta"]
        assert result.value.trace.metadata["streaming"] is True

    asyncio.run(scenario())


class FakeStreamingProvider(FakeProvider):
    def __init__(self, streams):
        super().__init__([])
        self.streams = list(streams)

    async def stream_chat(self, messages, tools=None, cancellation=None):
        self.messages.append((tuple(messages), tools))
        for event in self.streams.pop(0):
            yield event
```

- [ ] **Step 2: Run streaming test to verify RED**

Run:

```bash
uv run pytest tests/llm/test_llm.py::test_llm_step_streaming_provider_emits_delta_events_and_executes_tool_calls -q
```

Expected: FAIL importing `LlmStreamEvent` or calling `create_llm_step_function(..., stream=True)`.

- [ ] **Step 3: Implement stream event dataclass and step consumption**

In `src/loom/llm/api.py`, add:

```python
@dataclass(frozen=True, slots=True)
class LlmStreamEvent:
    kind: str
    content_delta: str | None = None
    reasoning_delta: str | None = None
    reasoning_context_delta: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    response: LlmResponse | None = None
    raw: Mapping[str, Any] | None = None
```

Change `create_llm_step_function(...):` signature:

```python
def create_llm_step_function(
    provider: Any,
    *,
    prompt_options: dict[str, Any] | None = None,
    enable_tool_calling: bool = True,
    max_tool_calls_per_step: int = 5,
    tool_selection: ToolSelectionConfig | None = None,
    tool_resolver: Any = None,
    stream: bool = False,
):
```

Replace:

```python
response = await provider.chat(messages, tools, getattr(runtime, "cancellation", None))
```

with:

```python
response = await _chat_or_stream(
    provider,
    messages,
    tools,
    getattr(runtime, "cancellation", None),
    runtime=runtime,
    context=context,
    trace_id=trace_id,
    llm_call_id=llm_call_id,
    stream=stream,
)
```

Add helper:

```python
async def _chat_or_stream(
    provider: Any,
    messages: list[LlmMessage],
    tools: tuple[dict[str, Any], ...] | None,
    cancellation: Any,
    *,
    runtime: Any,
    context: Context,
    trace_id: str,
    llm_call_id: str,
    stream: bool,
) -> Result:
    if not stream or not hasattr(provider, "stream_chat"):
        return await provider.chat(messages, tools, cancellation)
    return await _consume_streaming_chat(provider, messages, tools, cancellation, runtime, context, trace_id, llm_call_id)
```

Add `_consume_streaming_chat()`:

```python
async def _consume_streaming_chat(
    provider: Any,
    messages: list[LlmMessage],
    tools: tuple[dict[str, Any], ...] | None,
    cancellation: Any,
    runtime: Any,
    context: Context,
    trace_id: str,
    llm_call_id: str,
) -> Result:
    step_number = as_step_number(len(context.state.observations))
    started = await _emit_runtime_event(runtime, {"type": "llm.stream.started", "run_id": context.run_id, "loop_id": runtime.loop_id, "trace_id": trace_id, "llm_call_id": llm_call_id, "step_number": step_number, "model": provider.model, "at": runtime.now()})
    if not started.ok:
        return started
    final_response: LlmResponse | None = None
    try:
        async for event in provider.stream_chat(messages, tools=tools, cancellation=cancellation):
            if event.kind == "completed":
                final_response = event.response
                continue
            emitted = await _emit_stream_delta_event(runtime, event, context, trace_id, llm_call_id, provider.model, step_number)
            if not emitted.ok:
                return emitted
    except BaseException as exc:
        return err(make_loom_error("LLM_FAILED", str(exc), retryable=True, cause={"name": type(exc).__name__, "message": str(exc)}))
    if final_response is None:
        return err(make_loom_error("LLM_FAILED", "Streaming provider returned no completed response", retryable=True))
    completed = await _emit_runtime_event(runtime, {"type": "llm.stream.completed", "run_id": context.run_id, "loop_id": runtime.loop_id, "trace_id": trace_id, "llm_call_id": llm_call_id, "step_number": step_number, "model": provider.model, "at": runtime.now()})
    if not completed.ok:
        return completed
    return ok(final_response)
```

Add `_emit_stream_delta_event()`:

```python
async def _emit_stream_delta_event(runtime: Any, event: LlmStreamEvent, context: Context, trace_id: str, llm_call_id: str, model: str, step_number: int) -> Result:
    event_type = {
        "content.delta": "llm.content.delta",
        "reasoning.delta": "llm.reasoning.delta",
        "reasoning_context.delta": "llm.reasoning_context.delta",
        "tool_call.started": "llm.tool_call.started",
        "tool_call.arguments.delta": "llm.tool_call.arguments.delta",
        "tool_call.completed": "llm.tool_call.completed",
    }.get(event.kind)
    if event_type is None:
        return ok(None)
    return await _emit_runtime_event(
        runtime,
        {
            "type": event_type,
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "llm_call_id": llm_call_id,
            "step_number": step_number,
            "model": model,
            "delta": event.content_delta or event.reasoning_delta or event.reasoning_context_delta or event.tool_arguments_delta,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "raw": event.raw,
            "at": runtime.now(),
        },
    )
```

Set trace metadata:

```python
trace_metadata: dict[str, Any] = {
    "model": provider.model,
    "finishReason": final_response.finish_reason,
    "tokenUsage": _token_usage_metadata(tracker.total),
    "streaming": bool(stream and hasattr(provider, "stream_chat")),
}
```

Export `LlmStreamEvent` from `src/loom/llm/__init__.py` and `__all__`.

- [ ] **Step 4: Run streaming and existing LLM tests**

Run:

```bash
uv run pytest tests/llm/test_llm.py -q
```

Expected: existing tests plus new streaming test pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/loom/llm/api.py src/loom/llm/__init__.py tests/llm/test_llm.py
git commit -m "feat: stream llm step events"
```

---

### Task 4: OpenAI-Compatible SSE Streaming Parser

**Files:**
- Modify: `src/loom/llm/api.py`
- Test: `tests/llm/test_llm.py`

- [ ] **Step 1: Write failing OpenAI streaming provider test**

Add to `tests/llm/test_llm.py`:

```python
def test_openai_provider_stream_chat_parses_sse_chunks():
    async def scenario():
        calls = []

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "chunks": [
                    'data: {"choices":[{"delta":{"content":"{\\"reasoning\\":\\"ok\\","},"finish_reason":null}]}\n\n',
                    'data: {"choices":[{"delta":{"content":"\\"action\\":{\\"kind\\":\\"none\\",\\"description\\":\\"Stop\\"},\\"alternatives\\":[],\\"confidence\\":0.7}"},"finish_reason":null}]}\n\n',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}\n\n',
                    "data: [DONE]\n\n",
                ],
            }

        provider = create_openai_provider(
            api_key="test-key",
            model="gpt-test",
            base_url="https://proxy.example/v1",
            http_client=http_client,
        )

        events = [event async for event in provider.stream_chat([LlmMessage("user", "hello")])]

        assert calls[0][0] == "https://proxy.example/v1/chat/completions"
        assert calls[0][1]["body"]["stream"] is True
        assert [event.kind for event in events] == ["content.delta", "content.delta", "completed"]
        assert events[-1].response.content.startswith('{"reasoning":"ok"')
        assert events[-1].response.usage.total_tokens == 7

    asyncio.run(scenario())
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/llm/test_llm.py::test_openai_provider_stream_chat_parses_sse_chunks -q
```

Expected: FAIL with `AttributeError: 'OpenAIProvider' object has no attribute 'stream_chat'`.

- [ ] **Step 3: Implement provider streaming**

In `OpenAIProvider`, add:

```python
    async def stream_chat(self, messages, tools=None, cancellation=None):
        base_url = self.base_url.rstrip("/")
        body = {
            "model": self.model,
            "messages": [_to_openai_message(message) for message in messages],
            "stream": True,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
        request = {
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "body": body,
        }
        response = await self._send_stream(f"{base_url}/chat/completions", request)
        if not response.get("ok", False):
            status = int(response.get("status", 0))
            payload = response.get("json") or {}
            message = (payload.get("error", {}).get("message") if isinstance(payload, dict) else None) or f"OpenAI stream request failed with status {status}"
            raise RuntimeError(message)
        async for event in _parse_openai_sse_stream(response.get("chunks", ())):
            yield event

    async def _send_stream(self, url: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.http_client is not None:
            return await self.http_client(url, request)
        return await asyncio.to_thread(_send_stream_sync, url, request)
```

Add module helpers:

```python
async def _parse_openai_sse_stream(chunks: Any):
    content_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}
    finish_reason = "stop"
    usage = TokenUsage()
    async for payload in _iter_sse_payloads(chunks):
        if payload == "[DONE]":
            break
        data = json.loads(payload)
        choice = data.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason") or finish_reason
        usage_data = data.get("usage") or {}
        if usage_data:
            usage = TokenUsage(usage_data.get("prompt_tokens", 0), usage_data.get("completion_tokens", 0), usage_data.get("total_tokens", 0))
        content = delta.get("content")
        if content:
            content_parts.append(content)
            yield LlmStreamEvent(kind="content.delta", content_delta=content, raw=data)
        for stream_event in _parse_openai_stream_tool_deltas(delta, tool_parts, data):
            yield stream_event
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if reasoning:
            yield LlmStreamEvent(kind="reasoning.delta", reasoning_delta=reasoning, raw=data)
        reasoning_context = delta.get("reasoning_context")
        if reasoning_context:
            yield LlmStreamEvent(kind="reasoning_context.delta", reasoning_context_delta=reasoning_context, raw=data)
    yield LlmStreamEvent(
        kind="completed",
        response=LlmResponse(
            content="".join(content_parts) or None,
            tool_calls=tuple(_assembled_openai_stream_tool_calls(tool_parts)),
            usage=usage,
            finish_reason=finish_reason,
        ),
    )
```

Implement `_iter_sse_payloads`, `_parse_openai_stream_tool_deltas`, and `_assembled_openai_stream_tool_calls` in the smallest form needed by tests plus tool-call accumulation.

- [ ] **Step 4: Run provider streaming tests**

Run:

```bash
uv run pytest tests/llm/test_llm.py::test_openai_provider_stream_chat_parses_sse_chunks tests/llm/test_llm.py::test_openai_provider_request_parsing_and_error_mapping -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "feat: parse openai compatible llm streams"
```

---

### Task 5: TUI Stream Event Rendering

**Files:**
- Modify: `src/loom/tui/tui_collector.py`
- Modify: `src/loom/tui/tui_app.py`
- Test: `tests/unit/test_tui_app.py`

- [ ] **Step 1: Write failing stream formatting test**

Add to `tests/unit/test_tui_app.py`:

```python
def test_detail_panel_renders_llm_stream_deltas(monkeypatch):
    panel = DetailPanel()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.show_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.content.delta",
            data={"type": "llm.content.delta", "delta": '{"partial": true}', "llm_call_id": "call-1"},
            llm_call_id="call-1",
        )
    )
    panel.show_event(
        TuiEvent(
            timestamp=1,
            event_type="llm.tool_call.arguments.delta",
            data={
                "type": "llm.tool_call.arguments.delta",
                "delta": '{"query":"loom"}',
                "tool_name": "search",
                "tool_call_id": "tool-1",
            },
            llm_call_id="call-1",
            tool_call_id="tool-1",
        )
    )

    assert "LLM Stream" in writes[0]
    assert '"partial": true' in writes[0]
    assert "Tool Arguments" in writes[1]
    assert '"query": "loom"' in writes[1]
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/unit/test_tui_app.py::test_detail_panel_renders_llm_stream_deltas -q
```

Expected: FAIL because stream event detail falls through to generic formatting and does not include the expected headings.

- [ ] **Step 3: Implement TUI stream styles and details**

In `EVENT_STYLES`, add:

```python
    "llm.stream.started": {"icon": "◎", "color": COLORS["magenta"], "label": "LLM STREAM"},
    "llm.stream.completed": {"icon": "◉", "color": COLORS["magenta"], "label": "LLM DONE"},
    "llm.content.delta": {"icon": "…", "color": COLORS["magenta"], "label": "LLM TEXT"},
    "llm.reasoning.delta": {"icon": "…", "color": COLORS["blue"], "label": "LLM REASON"},
    "llm.reasoning_context.delta": {"icon": "…", "color": COLORS["blue"], "label": "LLM CTX"},
    "llm.tool_call.started": {"icon": "⚙", "color": COLORS["orange"], "label": "LLM TOOL"},
    "llm.tool_call.arguments.delta": {"icon": "⚙", "color": COLORS["orange"], "label": "TOOL ARGS"},
    "llm.tool_call.completed": {"icon": "⚙", "color": COLORS["green"], "label": "TOOL READY"},
```

In `_format_event_line()`, add compact labels for stream deltas:

```python
    elif event.event_type.startswith("llm.") and event.event_type.endswith(".delta"):
        delta = str(event.data.get("delta", ""))
        parts.append((delta[:60], COLORS["text_dim"]))
```

In `_format_event_detail()`, before the generic branch, add:

```python
    elif event.event_type in {"llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Stream ───[/]")
        delta = data.get("delta", "")
        if not _append_jsonish(lines, delta, indent="  "):
            _append_wrapped(lines, str(delta), indent="  ")
        lines.append("")

    elif event.event_type == "llm.tool_call.arguments.delta":
        lines.append(f"[bold {COLORS['orange']}]─── Tool Arguments ───[/]")
        tool = data.get("tool_name") or data.get("tool_call_id") or "unknown"
        lines.append(f"[dim]tool:[/] {tool}")
        delta = data.get("delta", "")
        if not _append_jsonish(lines, delta, indent="  "):
            _append_wrapped(lines, str(delta), indent="  ")
        lines.append("")
```

In `TuiEventCollector.emit()`, track stream duration:

```python
        elif event_type == "llm.stream.started":
            if llm_call_id:
                self._llm_request_times[llm_call_id] = now
        elif event_type == "llm.stream.completed":
            if llm_call_id and llm_call_id in self._llm_request_times:
                duration_ms = int((now - self._llm_request_times.pop(llm_call_id)) * 1000)
```

- [ ] **Step 4: Run TUI tests**

Run:

```bash
uv run pytest tests/unit/test_tui_app.py tests/unit/test_tui_plugin.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/loom/tui/tui_collector.py src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "feat: render streaming llm events in tui"
```

---

### Task 6: Real Project Smoke LLM Mode

**Files:**
- Modify: `src/loom/examples/real_project_smoke.py`
- Test: `tests/integration/test_real_project_smoke.py`

- [ ] **Step 1: Write failing LLM mode test**

Add to `tests/integration/test_real_project_smoke.py`:

```python
import json

from loom.llm import LlmResponse, LlmToolCall, TokenUsage
from loom.core.models import ok
```

Add fake provider:

```python
class FakeSmokeProvider:
    model = "fake-smoke-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        if self.calls == 1:
            return ok(
                LlmResponse(
                    content=None,
                    tool_calls=(
                        LlmToolCall("inspect-call", "inspect-project", "{}"),
                        LlmToolCall("smoke-call", "run-smoke-test", "{}"),
                        LlmToolCall("cli-call", "run-cli-smoke", "{}"),
                    ),
                    finish_reason="tool_calls",
                )
            )
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "reasoning": "I judged the evidence from the tools.",
                        "action": {
                            "kind": "custom",
                            "description": "Write the smoke audit report",
                            "input": {
                                "report": "# Fake LLM Smoke Report\n\nThe LLM made this judgment."
                            },
                        },
                        "alternatives": [],
                        "confidence": 0.83,
                    }
                ),
                usage=TokenUsage(10, 10, 20),
            )
        )
```

Add test:

```python
@pytest.mark.asyncio
async def test_real_project_smoke_llm_mode_uses_model_report(tmp_path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text("# Sample\n\nA tiny sample project.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")

    config = RealProjectSmokeConfig(
        target_path=project,
        smoke_command=("python", "-c", "print('smoke ok')"),
        cli_smoke_enabled=False,
        command_timeout_seconds=10,
    )
    provider = FakeSmokeProvider()

    result = await run_real_project_smoke(config, provider=provider, llm=True)

    assert result.ok
    assert result.value.output == "# Fake LLM Smoke Report\n\nThe LLM made this judgment."
    assert provider.calls == 2
    assert "Exclude .yakdb" not in result.value.output
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py::test_real_project_smoke_llm_mode_uses_model_report -q
```

Expected: FAIL because `run_real_project_smoke()` does not accept `provider` or `llm`.

- [ ] **Step 3: Implement LLM mode tools and loop**

In `src/loom/examples/real_project_smoke.py`, import:

```python
from loom.llm.api import create_env_openai_provider, create_llm_step_function
from loom.runtime.plugins import run_with_plugins
from loom.tui.plugin import TuiPlugin
```

Add tool handlers:

```python
def make_real_project_smoke_tools(config: RealProjectSmokeConfig):
    async def inspect_tool(_input_value, _options=None):
        info = inspect_project(config.target_path)
        return ok(Observation(new_trace_id(), "inspect-project", _project_info_value(info), now_iso()))

    async def smoke_tool(_input_value, _options=None):
        result = run_smoke_test(config)
        return ok(Observation(new_trace_id(), "run-smoke-test", _command_result_value(result), now_iso()))

    async def cli_tool(_input_value, _options=None):
        info = inspect_project(config.target_path)
        result = run_yakdb_cli_smoke(config, info)
        return ok(Observation(new_trace_id(), "run-cli-smoke", _cli_smoke_value(result), now_iso()))

    return {
        "inspect-project": inspect_tool,
        "run-smoke-test": smoke_tool,
        "run-cli-smoke": cli_tool,
    }
```

Add LLM context/loop helpers:

```python
def make_real_project_smoke_llm_context(config: RealProjectSmokeConfig):
    if not config.target_path.exists():
        return err(make_loom_error("VALIDATION_FAILED", "Real project smoke target does not exist", retryable=False, metadata={"target_path": str(config.target_path)}))
    return ok(
        freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(
                    role="real project smoke auditor",
                    constraints=(
                        {
                            "id": "llm-judgment-only",
                            "description": "Use tools for evidence, but make purpose, risk, and improvement judgments yourself.",
                            "severity": "must",
                        },
                    ),
                ),
                goal=GoalLayer(
                    objective=(
                        f"Audit {config.target_path}. Use available tools to collect evidence, then return JSON whose action.input.report is a markdown report with purpose, smoke result, risks, and improvement directions."
                    ),
                    budget={"max_steps": 1},
                ),
                state=StateLayer(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(
                    tools=(
                        ToolRef("inspect-project", "Inspect project metadata, README, files, and git status", input_schema={"type": "object", "properties": {}}),
                        ToolRef("run-smoke-test", "Run the configured smoke command and return stdout, stderr, exit code, and timing", input_schema={"type": "object", "properties": {}}),
                        ToolRef("run-cli-smoke", "Run project-specific CLI smoke if applicable and return command evidence", input_schema={"type": "object", "properties": {}}),
                    )
                ),
            )
        )
    )
```

Add:

```python
def make_real_project_smoke_llm_loop(config: RealProjectSmokeConfig, provider: Any, *, stream: bool = False) -> MinimalLoopDefinition:
    return MinimalLoopDefinition(
        id=new_loop_id(),
        version=new_loop_version(),
        identity=IdentityLayer(role="real project smoke auditor"),
        goal=GoalLayer(objective=f"LLM audit real project smoke path for {config.target_path}"),
        step=create_llm_step_function(provider, stream=stream),
        done=lambda context, _runtime: ok(bool(context.state.decisions)),
    )
```

Add:

```python
def _report_from_run_result(run_result) -> str:
    output = run_result.output
    if isinstance(output, dict):
        action = output.get("action", {})
        if isinstance(action, dict):
            input_value = action.get("input", {})
            if isinstance(input_value, dict) and isinstance(input_value.get("report"), str):
                return input_value["report"]
    latest = run_result.context.state.decisions[-1] if run_result.context.state.decisions else None
    if latest and isinstance(latest.action.input, dict) and isinstance(latest.action.input.get("report"), str):
        return latest.action.input["report"]
    return str(output)
```

Change `run_real_project_smoke()` signature:

```python
async def run_real_project_smoke(
    config: RealProjectSmokeConfig,
    *,
    provider: Any | None = None,
    llm: bool = False,
    tui: bool = False,
    stream: bool = False,
):
```

For non-LLM mode keep existing path. For LLM mode:

```python
    if llm:
        if provider is None:
            provider_result = create_env_openai_provider()
            if not provider_result.ok:
                return provider_result
            provider = provider_result.value
        context = make_real_project_smoke_llm_context(config)
        if not context.ok:
            return context
        handle = create(
            make_real_project_smoke_llm_loop(config, provider, stream=stream),
            registry=create_runtime_registry(tools=make_real_project_smoke_tools(config)),
        )
        if not handle.ok:
            return handle
        if tui:
            return await run_with_plugins(handle.value, context.value, max_steps=1, plugins=(TuiPlugin(),))
        return await run(handle.value, context.value, max_steps=1)
```

Make `main()` print `_report_from_run_result(result.value)` for LLM mode and keep existing deterministic output for non-LLM output if needed.

- [ ] **Step 4: Add CLI flags**

Modify `parse_args()`:

```python
    parser.add_argument("--llm", action="store_true", help="Use an LLM to judge evidence and write the report")
    parser.add_argument("--tui", action="store_true", help="Show live TUI events while the loop runs")
    parser.add_argument("--stream", action="store_true", help="Stream LLM deltas when provider supports it")
```

Return a tuple or add a separate `RealProjectSmokeRunOptions` dataclass:

```python
@dataclass(frozen=True, slots=True)
class RealProjectSmokeRunOptions:
    config: RealProjectSmokeConfig
    llm: bool = False
    tui: bool = False
    stream: bool = False
```

Keep compatibility by making `parse_args()` return `RealProjectSmokeRunOptions` and updating tests that call it, or use `parse_run_options()` and keep `parse_args()` for config only. Prefer `parse_run_options()` to avoid breaking existing tests:

```python
def parse_run_options(argv: tuple[str, ...] | list[str] | None = None) -> RealProjectSmokeRunOptions:
    ...
```

Then:

```python
def main(argv=None):
    options = parse_run_options(argv)
    result = asyncio.run(
        run_real_project_smoke(
            options.config,
            llm=options.llm,
            tui=options.tui,
            stream=options.stream or options.tui,
        )
    )
```

- [ ] **Step 5: Run real project smoke tests**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/loom/examples/real_project_smoke.py tests/integration/test_real_project_smoke.py
git commit -m "feat: add llm judged real project smoke mode"
```

---

### Task 7: Full Verification and Cleanup

**Files:**
- Modify only if verification reveals formatting/import issues.

- [ ] **Step 1: Run targeted suites**

```bash
uv run pytest tests/runtime/test_plugins.py tests/unit/test_tui_plugin.py tests/unit/test_tui_app.py tests/llm/test_llm.py tests/integration/test_real_project_smoke.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest
```

Expected: all tests pass, with live LLM tests skipped unless local credentials enable them.

- [ ] **Step 3: Run lint and format checks**

```bash
uv run ruff check src tests
uv run ruff format --check src tests
```

Expected: both commands pass.

- [ ] **Step 4: Run deterministic smoke demo**

```bash
uv run python -m loom.examples.real_project_smoke /Users/huanggui/workspace/yakDB --no-cli-smoke --timeout 120
```

Expected: command prints a deterministic markdown report and exits `0`.

- [ ] **Step 5: Run LLM mode help check**

```bash
uv run python -m loom.examples.real_project_smoke --help
```

Expected: output includes `--llm`, `--tui`, and `--stream`.

- [ ] **Step 6: Final status**

Run:

```bash
git status --short --branch
git log --oneline --decorate -5
```

Expected: worktree clean except intentional commits on `feature/tui-plugin-llm-streaming`.

---

## Self-Review

- Spec coverage: runtime plugin boundary is Task 1, TUI plugin is Task 2, streaming LLM events are Tasks 3-5, LLM-judged real project smoke mode is Task 6, verification is Task 7.
- Completeness scan: this plan contains no unfinished work items.
- Type consistency: `RunPluginContext`, `LoopPlugin`, `TuiPlugin`, `LlmStreamEvent`, `run_with_plugins`, and `run_real_project_smoke(..., llm, tui, stream)` are used consistently across tasks.
