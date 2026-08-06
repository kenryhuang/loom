# Evaluation Judge Streaming Design

## Problem

`loom.evaluation.analyze --judge` calls the configured judge provider through
`provider.chat()`. A reasoning model can spend a long time on each round or step
assessment, but the TUI receives no reasoning or content deltas while that call
is pending. Large traces therefore appear stalled even though judge requests are
still running.

Loom's agent loop already consumes `provider.stream_chat()`, assembles the final
`LlmResponse`, emits the standard streaming event protocol, preserves provider
errors, and applies tool-choice compatibility fallback. Judge calls should use
the same mechanism rather than maintain a second streaming implementation.

## CLI Contract

The evaluation analyzer adds an explicit Boolean pair:

```text
--stream | --no-stream
```

Streaming defaults to disabled for backward compatibility. It only changes how
LLM judge responses are transported and displayed; deterministic evaluation is
unchanged. `--stream` without `--judge` is accepted but has no effect because no
LLM call is made.

Example:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/yakdb-incremental-index-latest.jsonl \
  --out-dir .loom/yakdb-incremental-index/evaluation \
  --judge \
  --config config.yaml \
  --model kimi_judge \
  --stream \
  --tui
```

## Architecture

The LLM package will expose a reusable chat-or-stream operation. It accepts a
provider, messages, optional tools and tool choice, a cancellation value, a
stream flag, an event emitter, and stable event metadata. It returns one
assembled `LlmResponse` inside Loom's `Result` contract.

The operation owns only transport-level streaming events:

- `llm.stream.started`
- `llm.reasoning.delta`
- `llm.reasoning_context.delta`
- `llm.content.delta`
- `llm.tool_call.started`
- `llm.tool_call.arguments.delta`
- `llm.tool_call.completed`
- `llm.stream.completed`

Callers continue to own the surrounding semantic events:
`llm.requested`, `llm.completed`, and `llm.failed`. This keeps agent and judge
metadata meaningful while sharing response assembly, provider error handling,
and compatibility fallback.

The agent loop will adapt its runtime/context metadata to the reusable
operation without changing observable event payloads. `LlmRoundJudge` and
`LlmStepJudge` will pass their existing event metadata and the CLI stream flag.
Their existing JSON parsing and assessment models remain unchanged.

## Data Flow

For each round or step judge call:

1. Judge emits `llm.requested`.
2. The shared operation emits `llm.stream.started` and consumes provider SSE.
3. Each reasoning/content/tool delta is emitted immediately to the TUI.
4. The shared operation assembles the final `LlmResponse` and emits
   `llm.stream.completed`.
5. Judge emits `llm.completed` and parses the complete content using the
   existing assessment parser.
6. Provider or stream failures produce the existing `llm.failed` result path.

## Error and Cancellation Behavior

- A provider without `stream_chat` falls back to normal `chat` even when
  streaming is requested.
- Structured streaming HTTP errors retain status and provider diagnostics.
- Existing `tool_choice` compatibility fallback remains available to agent
  callers.
- Cancellation is forwarded through the shared operation when the provider
  supports it.
- A stream that ends without a completed response returns `LLM_FAILED`.
- Judge parse failures remain judge parse failures after the completed response;
  streaming does not change validation semantics.

## TUI Behavior

The shared TUI receives the same event types it already understands for agent
streaming. Reasoning and response deltas become visible during each judge call;
no evaluation-specific rendering path is required.

This change does not add judge concurrency, total `completed/total` progress,
timeouts, or durable judge checkpoints. Those are separate improvements.

## Tests

Tests will prove that:

1. CLI parsing supports `--stream` and `--no-stream` and defaults to false.
2. A streaming round judge emits the standard stream lifecycle and delta events
   before producing the same assessment as non-streaming mode.
3. A streaming step judge does the same.
4. A provider without streaming support falls back to `chat`.
5. Agent streaming event payloads and tool-choice fallback remain unchanged
   after extraction.
6. Evaluation without `--judge` remains deterministic and performs no provider
   call even if `--stream` is present.

## Scope

The implementation is limited to reusable LLM response streaming, evaluation
CLI wiring, and judge integration. It does not change scoring, prompts, model
configuration, judge ordering, task execution, evolution, or optimization.
