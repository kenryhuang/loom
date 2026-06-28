# TUI Plugin and Streaming LLM Design

## Purpose

Loom should support real-time loop observability without coupling the runtime engine to a terminal UI. A caller should be able to run any loop with `tui=True` or a TUI plugin and see every important run event, LLM interaction, tool call, and final decision as it happens.

The `real_project_smoke` demo should also have an LLM-first mode. Deterministic code may collect evidence from the target project, but the model must make the judgments: project purpose, smoke result interpretation, risks, and improvement directions.

## Current State

`src/loom/tui/demo.py` manually wires a `TuiEventCollector` into `run(..., trace_sink=collector)` and starts `LoomTuiApp` beside the loop task. `src/loom/tui/tui_runner.py` wraps the same pattern as `run_with_tui()`.

This already proves the right observation point: the runtime emits useful trace events, and TUI can consume them through the same `trace_sink` interface as persistence. The missing piece is a plugin boundary so a loop runner can opt into TUI without importing Textual or knowing about collector lifecycles.

`src/loom/llm/api.py` is async at the `chat()` boundary, but the response is not streamed. The runtime emits `llm.requested` and `llm.completed`, so TUI can only show provider I/O after the model call finishes. Tool calls are emitted by the runtime after response parsing, not while arguments are streaming.

## Design Principles

1. Runtime remains UI-agnostic.
   Textual belongs under `loom.tui`, not in `loom.runtime.engine`.

2. Streaming events are observability, not context.
   Token deltas and partial tool-call arguments should be live events. They must not inflate `Context.state` or become permanent reasoning history by default.

3. The TUI is fail-open.
   A rendering failure, queue overflow, or user quit should not change loop semantics unless the caller explicitly asks for strict observer behavior.

4. LLM judgment is separate from evidence collection.
   Tools can inspect files and run commands. The final report and recommendations must come from the LLM response in LLM mode, not from hard-coded project-specific heuristics.

5. Reasoning display is limited to provider-visible material.
   TUI may show provider-exposed reasoning summaries, reasoning context, content deltas, and tool-call deltas. It must not claim to display hidden chain-of-thought.

## Plugin Boundary

Add a runtime plugin module with a small protocol:

```python
@dataclass(frozen=True, slots=True)
class RunPluginContext:
    loop: LoopHandle
    initial_context: Context
    metadata: Mapping[str, Any]


class LoopPlugin(Protocol):
    async def start(self, context: RunPluginContext) -> Result: ...
    def trace_sink(self) -> Any | None: ...
    async def stop(self, result: Result | None) -> Result: ...
```

Add `run_with_plugins(...)` in `loom.runtime.plugins`. It will:

1. Build a `RunPluginContext`.
2. Start each plugin.
3. Collect plugin trace sinks.
4. Run the loop with a composite observer sink.
5. Stop plugins in reverse order.
6. Return the original loop result unless a strict plugin reports failure.

The existing `run()` function does not need to know about plugins. Plugin orchestration is a layer above the engine.

## TUI Plugin

Move the current `run_with_tui()` wiring into `TuiPlugin`:

```python
plugin = TuiPlugin(auto_exit_timeout_seconds=30.0)
result = await run_with_plugins(handle, context, plugins=(plugin,))
```

The plugin owns:

- `TuiEventCollector`
- `LoomTuiApp`
- app task lifecycle
- sentinel emission on stop
- optional fail-open behavior

`src/loom/tui/tui_runner.py` can remain as a convenience wrapper, implemented in terms of `run_with_plugins(..., plugins=(TuiPlugin(),))`.

## Streaming LLM Contract

Keep the existing `provider.chat()` API for compatibility and add an optional streaming API:

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

Supported event kinds:

- `content.delta`
- `reasoning.delta`
- `reasoning_context.delta`
- `tool_call.started`
- `tool_call.arguments.delta`
- `tool_call.completed`
- `completed`

If a provider has `stream_chat()`, `create_llm_step_function(..., stream=True)` consumes it. If not, the step function falls back to `chat()` and emits the existing request/completed events only.

The OpenAI-compatible provider should send `stream: true` when streaming is enabled and parse server-sent events incrementally through its injectable HTTP client. Tests should use a fake stream client and not require network access.

## Runtime Event Model

The LLM step should continue to emit stable lifecycle events:

- `llm.requested`
- `llm.completed`
- `llm.failed`

When streaming is active, it also emits transient events:

- `llm.stream.started`
- `llm.content.delta`
- `llm.reasoning.delta`
- `llm.reasoning_context.delta`
- `llm.tool_call.started`
- `llm.tool_call.arguments.delta`
- `llm.tool_call.completed`
- `llm.stream.completed`

The completed event remains the authoritative provider result. Delta events are for live observation. A trace sink policy can persist them, but the default TUI path should aggregate display state and avoid unbounded event growth.

## TUI Display Model

Keep the existing two-panel layout initially, but make it stream-aware:

- Timeline shows lifecycle events and compact stream markers.
- Detail panel appends request, response, tool input/output, and streamed deltas.
- LLM deltas are grouped by `llm_call_id`.
- Tool-call argument deltas are accumulated and pretty-printed when complete.
- Token metrics still come from final usage on `llm.completed`.

This avoids a full TUI redesign while making the core loop live enough for real debugging.

## Real Project Smoke LLM Mode

Add an LLM-backed execution path to `src/loom/examples/real_project_smoke.py`.

The evidence tools are deterministic:

- `inspect-project`
- `run-smoke-test`
- `run-cli-smoke`

The model receives those tools and an instruction to produce a JSON final decision containing:

```json
{
  "reasoning": "provider-visible summary of judgment",
  "action": {
    "kind": "custom",
    "description": "Write the smoke audit report",
    "input": {
      "report": "markdown report"
    }
  },
  "alternatives": [],
  "confidence": 0.0
}
```

The old deterministic `synthesize_report()` may stay for non-LLM mode, but LLM mode must not call `_recommendations()` or encode yakDB-specific conclusions. It should expose evidence and let the model decide.

CLI shape:

```bash
uv run python -m loom.examples.real_project_smoke /Users/huanggui/workspace/yakDB --llm --tui
```

`--llm` requires an environment-backed provider and fails clearly if model/base URL/API key are missing. There should be no silent deterministic fallback in LLM mode.

## Error Handling

- Missing provider config in `--llm` mode returns `VALIDATION_FAILED`.
- Streaming parse failure emits `llm.failed` and returns the provider error.
- Provider without streaming support works through `chat()` unless the caller explicitly requires streaming.
- TUI plugin defaults to fail-open. It records its own failure state and lets the loop finish.
- Tool failures are visible to the LLM as tool observations when possible. Runtime-level tool invocation failures still fail the step as they do today.

## Testing Strategy

1. Runtime plugin tests:
   Verify plugins start, receive trace events, stop, and do not change loop result in fail-open mode.

2. LLM streaming tests:
   Use a fake provider with `stream_chat()` and assert ordered `llm.stream.*`, delta, tool-call, and completion events.

3. OpenAI-compatible provider tests:
   Use an injected fake streaming HTTP client/SSE iterator. Verify request body includes `stream: true` and final chunks assemble into `LlmResponse`.

4. TUI collector/app tests:
   Verify stream delta events are normalized, grouped, and rendered in detail output without breaking existing event formatting.

5. Real project smoke LLM tests:
   Use a fake LLM provider that calls evidence tools and returns a final markdown report. Assert the report comes from the model response and deterministic yakDB recommendations are not injected.

## Non-Goals

- Full human chat inside the TUI.
- Replacing the existing trace store.
- Persisting every token delta by default.
- Exposing hidden chain-of-thought.
- Migrating from Chat Completions compatible providers to a different API surface in this change.
