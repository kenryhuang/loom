# Recoverable Tool Failures and Plan Update Barrier

## Status

Approved design for two failures observed in
`runs/yakdb-reliability-plan.jsonl` and the related event-feed follow behavior.

## Goal

Fix two related control-loop problems:

1. A normal tool validation or execution failure currently terminates the
   entire LLM step instead of returning the failure to the model for recovery.
2. Plan & Execute asks the model to update its checklist, but does not enforce
   an update between successive LLM tool batches.
3. The TUI currently forces the event feed to the bottom, or back to the plan
   node, even after the user scrolls upward to inspect earlier output.

The fixes must preserve the current ReAct loop, runtime trace events, planning
wrapper, and native/JSON-action tool-call paths.

## Non-Goals

- Do not make trace persistence, cancellation, or runtime infrastructure
  failures recoverable.
- Do not automatically retry failed tools.
- Do not infer plan item completion from tool names or outputs.
- Do not require an update between individual calls emitted by the same
  assistant response.
- Do not implement context compaction or task resume in this change.
- Do not change scrolling inside an individual event's detail pane.

## Recoverable Tool Failures

### Failure Domains

`runtime.call_tool()` continues to return `Result`. Errors produced after a
tool handler is resolved and invoked are annotated with
`failureDomain="tool"`. Missing tool handlers are also tool-domain failures
because the model can select another advertised action.

Failures from runtime event emission, trace persistence, cancellation, or
other execution infrastructure are not annotated as tool-domain failures.
`ABORTED` always terminates even if it originated while a handler was active.

### LLM Loop Behavior

When `create_llm_step_function()` receives a tool-domain error, it creates a
synthetic `Observation` containing:

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "old_text was not found",
    "retryable": false,
    "metadata": {
      "path": "app/storage/sqlite.py",
      "edit_index": 4,
      "occurrences": 0
    }
  }
}
```

For native tool calls, the value becomes the required `role=tool` result for
the originating tool-call ID. For JSON-action fallback, it enters the existing
tool-result feedback transcript. Both paths append the synthetic observation
to step observations.

The failed call counts toward `max_tool_calls_per_step`. It does not clear a
required-tool obligation merely by failing. The model decides whether to fix
the input, retry, use another tool, or finish with a blocker.

An unannotated error or `ABORTED` follows the existing terminal path.

## Plan Update Barrier

### Batch Identity

Every tool invocation made by the LLM adapter includes its current
`llm_call_id` in runtime metadata. Calls emitted by one assistant response
therefore share a stable batch ID.

### Barrier State

`PlanningRuntime` maintains run-local execution-batch state:

- active execution batch ID;
- whether a completed or failed normal-tool batch requires a plan update; and
- the plan revision that opened the batch.

This state is not global and does not alter the serialized `PlanState` schema.

### Enforcement

During `PlanPhase.EXECUTING`:

1. The first normal tool in a batch is allowed when the plan is clean.
2. Other normal tools with the same batch ID are allowed.
3. Any terminal normal-tool result, success or failure, marks the plan dirty.
4. A normal tool from a different batch is rejected while dirty.
5. Rejection returns the existing successful planning-guard observation with
   code `PLAN_UPDATE_REQUIRED`; the underlying handler is not invoked.
6. A successful `update_plan` clears the barrier.
7. Rejected calls do not open a new batch or change barrier state.

The planning workflow prompt explicitly says to call `update_plan` after each
normal-tool batch and before starting the next one. The update remains a full
checklist snapshot. The runtime does not fabricate checklist transitions.

The rule preserves multiple associated or parallel tool calls in one
assistant response while making checklist synchronization mandatory before
the next execution decision.

## Data Flow

```text
assistant response A
  -> normal tool A1
  -> normal tool A2
  -> terminal results
  -> plan dirty

assistant response B: normal tool
  -> planning.guard / PLAN_UPDATE_REQUIRED
  -> handler not invoked

assistant response C: update_plan
  -> revision increments
  -> barrier clears

assistant response D: normal tool
  -> executes
```

A tool-domain failure follows the same planning flow:

```text
tool.failed trace
  -> ToolFailureObservation returned to LLM
  -> plan dirty
  -> update_plan required before another normal-tool batch
```

## TUI Sticky-Tail Scrolling

### Follow State

`EventFeedWidget` owns a single sticky-tail state rather than leaving event
handlers to call `scroll_end()` independently. It starts in follow mode.

The feed pauses following when the user expresses upward navigation intent or
moves the viewport away from the bottom. This includes mouse-wheel or trackpad
scrolling, dragging the scrollbar, `k`, and `g`. Selecting an older visible
event with `k` pauses following even if the viewport has not moved yet.

Following resumes when the user manually returns the viewport to the bottom or
invokes `G`. Downward navigation with `j` resumes following once it reaches the
last event and the viewport reaches the bottom.

### Content Mutations

All feed mutations capture whether the feed was following before content or
layout changes. New events and growing LLM stream aggregates scroll to the new
end only when follow mode was active. Otherwise they preserve the user's
viewport.

`plan.updated` replaces the existing checklist content in place. It never
forces the plan node into view. When the user is following, the viewport stays
at the event-stream tail; when following is paused, the current viewport is
preserved.

Automatic scroll operations must not be interpreted as manual navigation.
The behavior is centralized behind `EventFeedWidget` methods so plan, LLM, and
tool event handlers cannot bypass the sticky-tail policy.

## Error Handling

- Tool-domain serialization must be JSON-safe and preserve the original code,
  message, retryable flag, and metadata.
- Failure conversion must not emit a second `tool.failed` event; the runtime
  event remains canonical.
- If error-observation construction itself fails, return an `INTERNAL` error.
- Plan guard rejections remain non-terminal observations so the model can
  comply on its next response.
- Tool-call budgets prevent repeated failed/rejected calls from looping
  without bound.

## Testing

### LLM Loop Tests

- A native `edit_file`-style validation failure appears in the next LLM
  request as a protocol-valid tool result.
- The model can issue a corrected tool call and complete the step.
- The synthetic failure is retained in step observations.
- JSON-action fallback receives equivalent failure feedback.
- Failed tools consume the configured tool-call budget.
- `ABORTED` and unannotated runtime failures still terminate immediately.
- The original `tool.failed` event is emitted exactly once.

### Planning Tests

- Multiple normal tools with one `llm_call_id` execute in the same batch.
- A different batch is rejected with `PLAN_UPDATE_REQUIRED` while dirty.
- The rejected handler is not invoked.
- A successful full-snapshot `update_plan` clears the barrier.
- A tool-domain failure also opens the barrier.
- Plan revisions and full-snapshot lifecycle events remain monotonic.
- Forced, auto, and off planning modes preserve their current entry behavior.

### Integration Test

Use a fake provider sequence equivalent to the observed yakDB failure:

1. submit and start a plan;
2. invoke a tool with invalid exact-match input;
3. receive the structured failure;
4. update the plan;
5. issue a corrected tool call; and
6. finish successfully.

Assert that the run does not fail at step 2, both tool attempts appear in the
trace, and a plan update occurs before the corrected execution batch.

### TUI Tests

- A newly appended event follows the tail when the viewport is already at the
  bottom.
- Scrolling upward prevents later events and stream updates from moving the
  viewport.
- `k` and `g` pause following even when the selected item was already visible.
- Returning to the bottom or invoking `G` resumes following.
- Updating a coalesced plan checklist does not jump back to the plan node.
- A plan update while following leaves the viewport at the stream tail.

## Acceptance Criteria

- A recoverable tool validation/execution failure no longer terminates the
  main LLM loop.
- System, trace, and cancellation failures remain terminal.
- Native and JSON-action paths expose the same structured error semantics.
- Plan & Execute prevents a new normal-tool batch until the model submits a
  full checklist update.
- Same-response multi-tool batches remain executable.
- The event feed follows new output only while sticky-tail mode is active.
- Manual review of earlier events is not interrupted by new events, streaming
  updates, or plan checklist revisions.
- Existing ReAct, planning, trace, and TUI tests pass.
