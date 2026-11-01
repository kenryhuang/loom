# TUI Semantic Event Summary Design

## Goal

Replace noisy, implementation-level timeline rows with concise summaries that explain task progress. The persisted JSONL trace remains complete and unchanged; filtering and summarization happen only in the TUI presentation layer.

The design must work identically for live events and replayed runs.

## Current Problem

The event formatter has no dedicated presentation for `decision.recorded`, `action.*`, or `observation.recorded`. Its generic fallback uses the event type as both title and description, producing rows such as:

```text
decision.recorded  decision.recorded
action.started     action.started
```

Tool rows are aggregated correctly by tool-call ID, but their summary always comes from input arguments. A completed tool therefore continues to show a command or path rather than its result. LLM request, stream, and response rows also emphasize transport details such as message and token counts instead of task progress.

The runtime events already contain better semantic data:

- Decisions contain an action description, reasoning, alternatives, and confidence.
- Actions contain a description, target, input, and completion outcome.
- Observations identify their source and may contain typed tool output or guard error codes and messages.
- Completed task tools return structured values such as exit code, duration, bytes read or written, replacements, changed-line location, and truncation state.

## Design Principles

1. Preserve every event in the JSONL trace.
2. Treat the TUI timeline as a curated progress view, not a raw event inspector.
3. Keep one visible row per meaningful unit of progress and update lifecycle rows in place.
4. Put concise outcomes on collapsed rows; retain full arguments, results, and reasoning in expandable details.
5. Use deterministic, local formatting. Do not add another LLM call to summarize events.
6. Apply the same policy to live collection and replay.

## Timeline Inclusion Policy

### Always visible

- Run lifecycle events.
- Step lifecycle events.
- Plan lifecycle snapshots.
- Decisions whose action description conveys intent.
- Aggregated tool executions.
- Guard failures, validation failures, and otherwise actionable observations.
- A final text answer.

### Hidden when redundant

- `action.recorded`, because it repeats the action already represented by the decision and action lifecycle.
- LLM observations that mirror the parsed decision.
- Tool observations already represented by the aggregated tool execution row.
- Plan-tool observations already represented by plan lifecycle events.
- LLM request transport rows.
- LLM responses containing only tool calls and no user-facing text.

### Conditionally visible

- `action.started` and `action.completed` are used only when no correlated decision or tool row already represents that action. Otherwise they are hidden.
- Other observations appear only when a semantic summary can be extracted and that summary is not already represented by another visible row.
- Unknown event types remain available in the detail/debug path but do not create `event_type event_type` timeline rows.

## Semantic Summaries

### Decision

Collapsed form:

```text
Decision  List top-level directory structure of yakDB
```

Use `decision.action.description` as the primary summary. Fall back to the action target, then to the first useful sentence of `decision.reasoning`. The detail area shows the full reasoning, confidence, selected action, target, input, and alternatives.

Decisions whose only description is a parser fallback such as `Use unstructured LLM response` are hidden when the corresponding LLM text or final answer is already visible.

### Tool execution

A tool call remains one row. `tool.started`, argument deltas, completion, and failure update that row in place.

While running, show a compact operation and target rather than serialized arguments:

```text
Bash   Running tests
Read   wal.rs
Edit   wal.rs
Write  reliability-report.md
```

On completion, prefer structured output:

```text
Bash   Passed · exit 0 · 43 passed, 8 skipped · 3.41s
Read   wal.rs · 238 lines · 8.4 KB
Edit   wal.rs · 1 replacement · first change line 119 · 8.1 KB
Write  reliability-report.md · 6.2 KB
Finish Completed
```

Shell summaries use exit code, timeout state, duration, and a concise signal parsed from the final meaningful stdout or stderr lines. Test-count patterns are recognized when present. Arbitrary output is truncated to one normalized line. A non-zero exit code is a failed outcome even when the runtime tool wrapper returned an observation successfully.

Generic tools use common structured fields in priority order: `summary`, `message`, `status`, `result`, count fields, then a compact scalar. They fall back to an operation target, not a full argument dump.

On failure, show the stable error code and first meaningful message. The detail area retains complete input, output, diff, stdout, stderr, and error metadata.

### Observation

Only actionable or otherwise novel observations produce a row. Guard observations use:

```text
Plan blocked  PLAN_UPDATE_REQUIRED · Update the complete plan checklist…
```

Validation and execution errors use the source, code, and first meaningful message. Successful observations from `read_file`, `edit_file`, `write_file`, `shell_execute`, `finish`, `submit_plan`, or `update_plan` are hidden because their information is already represented elsewhere.

### LLM progress and answer

Transport bookkeeping is not shown as separate Request and Response rows.

During streaming, one `Thinking` row is updated in place with the first meaningful normalized sentence from reasoning. Duration and token usage remain secondary metadata, available in details.

A response containing only tool calls is absorbed into the tool rows. A response with user-facing text becomes:

```text
Answer  Successfully fixed WAL size tracking in PeriodicSync mode…
```

The full answer remains expandable.

### Plan, run, and step

Existing plan checklist rendering and plan lifecycle history remain unchanged. Run and step rows remain visible as structural boundaries. Their descriptions use known outcomes and counts and never repeat the event type.

## Presentation Architecture

Introduce two small presentation concepts inside the TUI layer:

1. An inclusion classifier decides whether an event should create or update a visible row, be absorbed by another row, or be omitted as redundant.
2. A semantic summarizer returns a title, concise description, color/status, and optional detail model from typed payload fields.

Existing LLM-stream and tool-execution state continue to perform lifecycle aggregation. The classifier runs on their aggregated event rather than on every raw delta. Correlation uses existing IDs first (`llm_call_id`, `tool_call_id`, and `trace_id`) and source/tool identity second; no runtime schema change is required.

The event feed continues to retain every visible semantic event for the full scrollable timeline. Filtering must not reintroduce the prior bounded-history behavior.

## Error Handling and Fallbacks

- Malformed or unexpected payloads must never crash rendering.
- Missing descriptions fall back through known semantic fields; if none exist, omit the noisy row rather than repeat the event type.
- Failed events always remain visible, even when their successful counterpart would be hidden.
- Unrecognized tool output uses a compact scalar or status summary while full data remains in details.
- Markup and long text continue through existing escaping and truncation helpers.

## Testing

Unit tests will cover:

- Meaningful decision summaries and expanded decision details.
- Suppression of duplicate action and observation lifecycle events.
- Visibility of guard and validation observations.
- Running, successful, timed-out, non-zero-exit, and runtime-failed shell summaries.
- Read, edit, write, finish, planning, and generic tool summaries.
- Tool lifecycle aggregation updating one row in place.
- Suppression of tool-call-only LLM responses and display of final text answers.
- Safe behavior for missing or malformed fields.
- Identical results for replayed trace events and live events.
- Preservation of complete visible history and sticky-tail behavior.

An integration-style replay test will use representative payload shapes from `runs/yakdb-reliability-plan.jsonl` without copying large command output into fixtures.

## Out of Scope

- Changing runtime event schemas or persisted JSONL data.
- AI-generated event summaries.
- Step-level cards that merge an entire execution batch.
- Removing the expandable raw detail view.
- Changing plan checklist rendering or scroll-follow behavior.
