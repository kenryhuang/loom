# TUI Thinking Metrics Design

## Status

Approved design for restoring metric-only LLM thinking rows in the task TUI.

## Goal

Show each aggregated LLM streaming event using the original compact form:

```text
Thought for 13s 38 tokens >
```

The row must communicate elapsed time and streamed token/delta count without
including model reasoning or response-content excerpts.

## Behavior

- LLM stream lifecycle events remain aggregated into one timeline row per
  `llm_call_id`.
- The row title is `Thought for <elapsed>`.
- The row description is `<count> token >` when the count is one and
  `<count> tokens >` otherwise.
- Elapsed time comes from the aggregated event's `elapsed_ms`, falling back to
  `duration_ms` through the existing duration formatter.
- The count comes from `token_count`, falling back to `delta_count`.
- While the stream is active, the same row continues to update as elapsed time
  and token count change.
- Reasoning, content, reasoning-context, and delta text are not used in the
  timeline-row summary. They remain available to existing detail rendering and
  trace persistence.
- All non-thinking event summaries retain their current behavior.

## Implementation Boundary

Change only the LLM stream branch of `_event_conversation_parts` in
`src/loom/tui/tui_app.py`. The existing `_LlmStreamState` aggregation and
event-feed replacement behavior remain unchanged. Remove the now-unused stream
text-preview helper if it has no remaining callers.

## Testing

- Assert the exact metric-only text for an aggregated completed stream.
- Assert singular `token` grammar for a count of one.
- Assert reasoning/content text is not included in the row.
- Update the semantic timeline replay expectation from `Thinking ...` to the
  metric-only form.
- Run the focused TUI tests and the complete project test suite.

## Non-Goals

- Do not change provider usage accounting or the bottom status bar.
- Do not replace the streamed delta count with final provider billing usage.
- Do not change LLM answer, tool, plan, decision, or action summaries.
