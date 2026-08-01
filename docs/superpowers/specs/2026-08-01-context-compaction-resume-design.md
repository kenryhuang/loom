# Context Compaction and Resumable Task Runs

## Status

Approved design. This document defines model-context compaction and
checkpoint-based recovery for Loom's generic task runner.

## Goal

Bound the model-visible context used by `loom run task` without losing the
append-only execution evidence in Loom traces. A task must also be able to
resume in another process from its latest valid checkpoint without replaying
completed tool side effects.

The design applies to the existing ReAct loop in
`create_llm_step_function()`. It must work with the Plan & Execute wrapper and
must not replace Loom's `Context`, `Trace`, `MinimalLoopDefinition`, runtime
engine, or tool registry.

## Current Problem

`create_llm_step_function()` starts an outer runtime step by building two
messages from `Context`, then repeatedly:

1. calls the LLM;
2. appends the assistant response;
3. executes tool calls;
4. appends every tool result; and
5. calls the LLM again.

`TaskHarness.max_history_steps` limits the observations and decisions projected
from earlier outer steps. It does not bound the assistant/tool transcript that
grows inside one outer step. A complex task can therefore exhaust the model's
context window before the step returns.

The current JSONL trace records events and completed traces, but it is not a
recoverable task session. It does not persist a typed active message window,
an in-flight step continuation, or a complete serialized `Context` from which
another process can continue.

## Reference Mechanisms

The design borrows the following behavior from Codex:

- compaction is a persisted history-replacement checkpoint, not trace deletion;
- automatic compaction may happen before a turn or during a tool-follow-up
  turn;
- stable initial instructions are re-injected after replacement;
- replacement history and its window lineage are persisted for recovery;
- automatic compaction is driven by model context limits with reserved
  headroom; and
- completed context windows remain auditable even though the model no longer
  receives their complete contents.

It borrows the following behavior from Pi:

- preserve a configurable recent-token suffix;
- update a previous structured summary incrementally;
- never separate a tool result from its originating tool call;
- serialize tool results with strict bounds for summarization;
- distinguish threshold compaction from overflow recovery;
- allow only one compact-and-retry overflow recovery; and
- persist the summary and the first retained boundary separately from the full
  session history.

Loom does not copy either repository's session model. The mechanism is adapted
to Loom's explicit `Context`, `KnowledgeLayer`, runtime events, planning
wrapper, and trace-first architecture.

## Non-Goals

- Do not redesign the outer runtime step contract.
- Do not promote every LLM/tool round into a separate outer step.
- Do not delete, rewrite, or compact the trace file itself.
- Do not create an independent long-term memory store disconnected from
  `KnowledgeLayer` and trace provenance.
- Do not automatically replay a tool call whose side-effect status is
  uncertain.
- Do not support resuming a terminal run.
- Do not add branch navigation or branch summarization.
- Do not require a separate summary model.

## Chosen Architecture

Use a dedicated `ContextManager` injected into the existing LLM step. The LLM
step retains ownership of ReAct control flow; the manager owns model-visible
window projection, token pressure, compaction, safe checkpoints, and restored
continuations.

The four state categories are deliberately separate:

```text
Trace
  append-only facts: LLM requests/responses, tool calls/results, compaction

Context
  Loom domain state: goal, plan, observations, decisions, stable knowledge

ContextWindow
  bounded prompt projection: pinned context, summary, recent exchanges

Checkpoint
  recoverable projection: Context, ContextWindow, continuation, trace cursor
```

Suggested module boundaries:

```text
src/loom/llm/context_window.py
  ContextWindow contracts, token estimation, cut selection, summary protocol

src/loom/tasks/context_manager.py
  task Context projection, policy orchestration, compaction and Context updates

src/loom/runtime/checkpoints.py
  checkpoint participant/store protocols, codecs, validation and recovery

src/loom/observability/traces.py
  append-only compaction/checkpoint events and trace cursor support
```

`llm/api.py` calls the manager lifecycle. It does not implement summarization,
checkpoint encoding, or recovery policy.

## Core Contracts

### ContextCompactionConfig

The policy is an immutable configuration value:

```python
@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    mode: Literal["auto", "force", "off"] = "auto"
    context_window: int | None = None
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    max_message_tokens: int = 10_000
    summary_model: str | None = None
    max_overflow_recoveries: int = 1
```

`force` compacts at the first safe point that has eligible history. Its purpose
is deterministic verification. `off` disables history replacement but does not
disable per-message hard bounds.

`context_window` may come from explicit task configuration or provider model
metadata. If neither source provides it, threshold prediction is disabled;
per-message bounds and provider-reported overflow recovery remain active.

`GoalLayer.budget.max_tokens` remains a cumulative task/step budget. It is not
the model context window. Summary calls count toward token, cost, and duration
budgets.

### ContextWindow

The active window is typed and segmented:

```text
ContextWindow
├── window_id
├── window_number
├── pinned
├── summary
├── recent_exchanges
├── pending_exchange
└── accounting
```

`pinned` contains the stable system prompt, task objective, constraints,
current plan, current Context projection, and tool contract. It is rebuilt at
outer-step boundaries and never summarized.

`summary` contains zero or one validated `CompactionSummary`.

`recent_exchanges` contains complete model-visible message groups after the
current summary boundary. Each exchange has stable message identifiers and
source event references.

`pending_exchange` contains the current assistant tool-call batch until every
call has a terminal result or an interruption result. A pending exchange is
not an eligible cut boundary.

`accounting` records actual prompt usage when available and estimates for
messages appended since that response.

### CompactionSummary

Summaries are validated structured data:

```text
CompactionSummary
├── objective
├── constraints_and_preferences
├── plan_snapshot
├── completed_work
├── active_work
├── key_decisions
├── verified_facts
├── files_and_commands
├── failures_and_recovery
├── next_actions
└── source_refs
```

The summary model returns this schema. Loom extracts trace IDs, tool-call IDs,
file paths, commands, result status, and plan revision from events and merges
those fields deterministically. The LLM cannot invent provenance.

The structured value is rendered deterministically into one model message.
It is also stored as a `KnowledgeItem(kind="memory")` when the enclosing outer
step produces its next immutable `Context`. The memory records its source
trace, source window, summary schema version, and confidence.

### StepContinuation

An in-flight continuation contains enough state to resume without recreating
already completed work:

```text
StepContinuation
├── phase
├── trace_id
├── llm_call_count
├── tool_call_count
├── pending_required_tools
├── last_assistant_response
├── completed_tool_observations
├── unresolved_tool_calls
└── overflow_recovery_count
```

Allowed phases distinguish at least `before_llm`, `after_llm`, `tool_batch`,
and `step_finalizing`. A checkpoint is resumable only at a documented safe
phase.

### CheckpointParticipant

Runtime extensions with live state implement a small protocol:

```python
class CheckpointParticipant(Protocol):
    id: str
    schema_version: int

    def snapshot(self) -> JsonValue: ...
    def restore(self, value: JsonValue) -> Result: ...
```

`PlanningRuntime` participates through this protocol. Its snapshot is the
canonical controller state, including plan ID, phase, revision, item statuses,
and next item ID. This handles a crash after a plan tool transition but before
the planning wrapper has copied the state into `Context.state.scratch`.

## ContextManager Lifecycle

`run_generic_task()` constructs one manager per run and injects it into
`create_llm_step_function()`.

The LLM step uses the following conceptual lifecycle:

```text
open_step(context, runtime)
before_llm_request(session)
after_llm_response(session, response)
after_tool_result(session, tool_call, observation)
close_tool_batch(session)
finalize_step(session, next_context)
```

`open_step()` either creates a window from the current `Context` or restores a
validated continuation.

`before_llm_request()` closes any terminal exchange, estimates the exact
request projection, compacts when required, and commits a safe checkpoint
before sampling.

`after_llm_response()` records the assistant response and starts a pending
tool exchange when tool calls exist.

`after_tool_result()` records each terminal observation. The trace event must
be durable before the continuation checkpoint can claim the result.

`close_tool_batch()` commits the completed exchange and establishes the next
safe checkpoint.

`finalize_step()` adds compaction memories and lightweight window metadata to
the new immutable Context. The complete transcript remains outside
`StateLayer.scratch`; scratch stores only its plan state and a lightweight
checkpoint/window reference.

Callers that do not pass a manager retain the existing behavior.

## Token Accounting and Triggering

Before each LLM request, the manager calculates active context usage as:

```text
last provider-reported prompt usage
+ estimated tokens for messages appended after that response
+ current request-only prompt/tool overhead not present in the last response
```

If no valid provider usage exists, all active items are estimated. Estimation
must be deterministic and conservative. Provider-aware tokenizers may replace
the estimator without changing policy contracts.

Automatic compaction triggers when projected usage exceeds:

```text
context_window - reserve_tokens
```

It also triggers when:

- the provider reports a context overflow;
- the active model changes to a smaller context window;
- a model compaction-compatibility identifier changes; or
- `mode=force` reaches its first eligible safe point.

An oversized individual item is bounded before it enters the window. This is
not deferred until overall token pressure occurs.

## Compaction Algorithm

1. Preserve all pinned items.
2. Close the pending exchange or wait until it has terminal synthetic results.
3. Walk complete recent exchanges from newest to oldest until approximately
   `keep_recent_tokens` have been retained.
4. Select the oldest retained exchange as the new boundary.
5. Serialize older exchanges for the summary model. Tool results receive a
   stricter summarization excerpt limit than model-visible results.
6. Pass the previous structured summary and only the newly compacted exchanges
   to the summary model.
7. Validate the result and merge deterministic provenance.
8. Build a replacement window from pinned items, the updated summary, and the
   retained exchanges.
9. If the replacement still exceeds the threshold, reduce the retained suffix
   until it fits or reaches zero.
10. Commit the replacement window and checkpoint before the next LLM request.

A complete assistant tool-call message and all of its corresponding results
form one atomic exchange. Compaction never leaves the model with an unmatched
tool result or a tool call missing its results.

Repeated compactions update the previous summary rather than summarizing all
historical trace events again.

## Large Tool Results

Every model-visible message has a hard limit. For an oversized tool result:

- the full value remains in `tool.completed` trace evidence;
- the active window contains a deterministic bounded excerpt;
- the projection states that it was truncated;
- the projection records the original byte/token estimate and trace/tool-call
  reference; and
- an artifact reference is included when the tool publishes one.

The initial implementation may use trace references when no general artifact
store is available. It must not invent an artifact abstraction solely for
compaction.

Summary serialization applies a smaller limit to each tool result so that the
compaction request itself cannot be dominated by the content being compacted.

## Summary Model and Failure Handling

The current task model is the default summary model. A configured
`summary_model` may select another model through the existing task model
configuration path.

Summary calls use a separate request/session identity where the provider
supports it. They do not disturb the main conversation's request-local state.

Failure policy:

- retry transient summary transport failures using the normal bounded retry
  policy;
- retry an invalid structured summary once with validation feedback;
- fall back to a deterministic extractive summary if validation still fails;
- if threshold compaction fails before the hard limit, allow at most one
  uncompressed request when it still fits;
- if overflow caused compaction, permit only one compact-and-retry recovery;
- if pinned content alone exceeds the hard limit, return
  `CONTEXT_WINDOW_EXCEEDED`; and
- if a resumable run cannot persist a safe checkpoint after side effects,
  stop the run instead of continuing in an unrecoverable state.

Suggested new error codes are `CONTEXT_WINDOW_EXCEEDED`,
`CONTEXT_COMPACTION_FAILED`, and `CHECKPOINT_INVALID`.

## Run Bundle and Persistence

A resumable run uses a directory:

```text
run/
├── manifest.json
├── trace.jsonl
└── checkpoint.json
```

`manifest.json` identifies the task request, resolved workspace, loop ID and
version, tool schema digests, prompt template versions, model configuration,
compaction policy, and checkpoint schema version.

`trace.jsonl` remains append-only. Records receive a monotonic sequence and a
hash that can be referenced as a durable cursor.

`checkpoint.json` contains:

```text
schema_version
checkpoint_id
run_id
attempt_id
loop_identity
serialized_context
context_window
step_continuation
token_accounting
trace_cursor
participant_snapshots
created_at
```

The Context codec must round-trip every core layer used by generic tasks,
including `FrozenDict`, observations, decisions, knowledge items, affordances,
and plan scratch data. Checkpoint decoding is schema-versioned and rejects
unknown required shapes.

### Atomic Commit

Checkpoint commit order is:

1. append and flush all LLM, tool, plan, and compaction events represented by
   the checkpoint;
2. build a checkpoint referencing the final durable trace sequence and hash;
3. write a temporary checkpoint, flush it, and atomically rename it over
   `checkpoint.json`; and
4. append `context.checkpoint.committed` for observability.

A crash between steps 3 and 4 leaves a valid checkpoint. A crash between
steps 1 and 3 leaves durable trace tail records beyond the last committed
cursor. Recovery reduces those records deterministically before deciding how
to continue.

## Recovery Semantics

Recovery keeps the original `run_id` and assigns a new `attempt_id`. It emits
`run.resumed` before normal execution continues.

Recovery validates:

- checkpoint schema and internal hashes;
- the checkpoint's referenced trace cursor;
- task and workspace identity;
- loop and tool compatibility;
- prompt and summary schema compatibility; and
- all checkpoint participant versions.

If validation fails, recovery returns `CHECKPOINT_INVALID`; it does not guess
or silently start a new run.

The recovery reducer reads trace records after the checkpoint cursor:

- a tool call with `tool.completed` or `tool.failed` is terminal and its result
  is restored without replay;
- a call with `tool.started` and no terminal event becomes
  `interrupted/unknown`;
- calls from the same assistant batch that never started become
  `not_executed_due_to_interruption`; and
- synthetic terminal tool messages are created for interrupted and unexecuted
  calls so the next LLM request receives a valid tool protocol transcript.

After recovery, the LLM decides whether to inspect external state, retry a
safe operation, choose another action, or report a blocker. Loom never
automatically replays an uncertain side effect.

Terminal runs cannot be resumed. A future fork operation may create a new run,
but it is outside this design.

Model changes are allowed when the task and tool contract remain compatible.
They emit a model-change event. A smaller context window or changed compaction
compatibility identifier forces compaction before the first resumed sampling
request.

## Plan & Execute Integration

The planning wrapper continues to decorate `MinimalLoopDefinition`; the ReAct
path is not duplicated.

`PlanningRuntime` registers as a checkpoint participant. Its live controller
snapshot is committed at every safe point. On resume it restores that snapshot
before it projects planning tools and workflow constraints.

The restored planning state must agree with any later plan events in the trace
tail. The same recovery reducer applies later events monotonically. A stale
plan tool advertised before a crash remains subject to the existing planning
controller guards.

## Trace and TUI Events

Add these domain events:

```text
context.compaction.started
context.compaction.completed
context.compaction.failed
context.checkpoint.committed
run.resumed
tool.interrupted
```

`context.compaction.completed` includes reason, source and replacement window
IDs, before/after active tokens, summarized and retained exchange counts,
summary schema version, summary model usage, fallback status, and checkpoint
ID. It references source events instead of repeating full tool outputs.

The TUI creates one Compaction node at `started` and updates its content area
at completion or failure. The completed content shows:

- trigger reason;
- before/after tokens;
- summarized and retained exchange counts;
- Goal, Progress, Decisions, and Next Steps from the summary; and
- checkpoint ID.

Checkpoint commits are collapsed by default so a checkpoint after each tool
batch does not flood the event list.

`run.resumed` creates a Resume node showing the checkpoint, attempt, recovered
plan phase, and counts of restored, interrupted, and unexecuted tool calls.

The status area shows active context usage and its compaction threshold
separately from cumulative task token usage.

## CLI and Configuration

New CLI behavior:

```bash
loom run task "Inspect and update this project" \
  --run-dir runs/project-update \
  --context-compaction auto

loom run task --resume runs/project-update
```

`--context-compaction` accepts `auto`, `force`, or `off`.

`--run-dir` enables the complete run bundle and recovery lifecycle. Existing
`--trace-path` remains available for one-shot compatibility. Supplying
`--resume` loads the task request and normal execution settings from the run
manifest; incompatible CLI overrides are rejected explicitly.

Configuration adds context policy fields under `run`. Model configuration may
provide `context_window`, compaction compatibility metadata, and an optional
named summary model. Secrets are not copied into the manifest or checkpoint.

## Testing Strategy

### Unit Tests

- real usage plus trailing estimates produces deterministic token pressure;
- threshold, force, off, overflow, and model-downshift triggers behave as
  specified;
- cut selection retains the configured suffix and never splits a tool
  exchange;
- oversized tool results produce bounded projections with valid references;
- repeated compactions update the previous summary;
- invalid summaries retry once and then use deterministic fallback;
- Context, ContextWindow, continuation, and participant snapshots round-trip;
- checkpoint hashes and trace cursors reject corruption; and
- trace-tail reduction classifies completed, interrupted, and unexecuted tools.

### Integration Tests

- one outer ReAct step with many LLM/tool rounds compacts before overflowing;
- the next request contains pinned context, one summary, and retained exchanges
  but not compacted raw exchanges;
- the trace still contains all original LLM and tool evidence;
- provider context overflow causes one compact-and-retry attempt;
- Plan & Execute state survives a checkpoint and process recreation;
- switching to a smaller-context model compacts before sampling;
- existing callers without a ContextManager preserve current behavior; and
- existing task, planning, trace, and CLI tests remain compatible.

### Crash Injection Tests

Inject process-equivalent failures at these boundaries:

- after `tool.started`;
- after `tool.completed` but before checkpoint replacement;
- after checkpoint replacement but before the commit event;
- during summary generation; and
- after a plan transition but before outer-step finalization.

Every recovery test asserts that completed tools are not replayed, ambiguous
tools receive synthetic interruption results, plan revisions remain monotonic,
and the recovered prompt is protocol-valid.

### TUI Tests

Use content-area and full-node snapshots for compaction started/completed,
compaction failure, and run resume. Verify checkpoint events remain collapsed
and token metrics do not conflate active context with cumulative usage.

## Implementation Stages

1. Add `ContextWindow`, structured summaries, token policy, atomic exchange
   selection, and bounded tool-result projections.
2. Add `ContextManager` lifecycle injection to the current ReAct loop,
   automatic compaction, and overflow recovery.
3. Add the run bundle, Context/checkpoint codecs, trace cursor, recovery
   reducer, and planning checkpoint participant.
4. Add CLI/config/TUI integration and crash-injection coverage.

Each stage must preserve the existing ReAct and Plan & Execute behavior before
the next stage starts.

## Acceptance Criteria

- Long inner ReAct transcripts remain below the configured model context
  threshold.
- No model-visible item is unbounded.
- Compaction produces a validated structured summary with trace provenance.
- Full pre-compaction evidence remains available in the append-only trace.
- Repeated compactions retain recent work and incrementally update prior
  summaries.
- A task can resume in another process from its latest valid checkpoint.
- Completed tool side effects are never replayed automatically.
- Interrupted or uncertain tools are returned to the LLM as explicit synthetic
  results.
- Plan & Execute state resumes without phase or revision regression.
- Compaction, checkpoint, and resume behavior is visible and testable through
  events and the TUI.
- Existing non-resumable task callers remain compatible.
