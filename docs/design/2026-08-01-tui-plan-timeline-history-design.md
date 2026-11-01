# TUI Plan Timeline History

## Status

Approved design based on the completed yakDB reliability run in
`runs/yakdb-reliability-plan.jsonl`.

## Problem

The TUI currently keeps one event-feed node per `plan_id`. Every later
`plan.submitted`, `plan.updated`, and `plan.completed` event replaces that
node's content in place.

For run `run_19fbb611a4c_2_497cfc48`, the trace order is:

```text
event 3   plan.entered revision 0
event 10  plan.submitted revision 1
event 28  plan.updated revision 2
event 59  plan.updated revision 3
event 80  plan.completed revision 4
```

The original node remains at event 3's visual position but eventually displays
`Plan completed · revision 4`. This makes the top of a long run look as though
the plan was already complete and the intervening history was evicted.

There is no feed capacity limit or oldest-node eviction. Replaying the run's
96 runtime events through the current TUI produces 67 logical nodes. The 29
fewer nodes are accounted for by intentional aggregation and suppression:

- 11 LLM stream-start nodes are updated into their completed stream nodes;
- tool start/result phases are combined and successful internal planning-tool
  executions are hidden; and
- five plan lifecycle events are overwritten into one node.

Only the last behavior violates the intended chronological presentation.

## Goal

Display every plan lifecycle transition at its actual chronological position,
with the complete checklist snapshot from that revision, while preserving the
existing useful aggregation for short-lived LLM streams and tool calls.

## Non-Goals

- Do not display every token delta as a separate row.
- Do not split `tool.started` and its terminal result into separate rows.
- Do not expose successful internal `enter_plan`, `submit_plan`, or
  `update_plan` tool executions in addition to their lifecycle events.
- Do not add event-feed retention limits, pagination, or virtualization.
- Do not change checklist formatting or status icons.
- Do not change sticky-tail scrolling semantics.

## Timeline Model

### Plan Events

Each event in `PLAN_PRESENTATION_EVENTS` appends a new `EventItem`:

- `plan.entered`;
- `plan.submitted`;
- every `plan.updated`; and
- `plan.completed`.

No plan event updates or selects an earlier node. Each row retains the event's
own revision, phase, explanation, reason, and full checklist snapshot. The
visual order therefore matches trace order exactly.

The app no longer needs a `plan_id -> event index` map because plan nodes are
immutable after insertion.

### LLM and Tool Aggregation

LLM streaming remains one logical Thought row per `llm_call_id`. Deltas update
that live row and the completed stream freezes its final accumulated content.

Tool execution remains one logical row per tool-call identity. Started,
completed, and failed phases are combined into that row. Successful internal
planning-tool rows remain suppressed because the corresponding plan lifecycle
event is the canonical user-facing record. Rejected or failed planning tools
remain visible.

These operations are short-lived and retain their complete accumulated result,
unlike a plan lifecycle that spans the entire task and has meaningful historic
snapshots.

## Scrolling and Selection

Plan events use the same `EventFeedWidget.add_event()` path as other immutable
timeline entries.

- In sticky-tail mode, the new plan row becomes the latest selected event and
  the feed follows the bottom.
- When the user has paused following, the plan row is appended without changing
  the current selection, expansion state, or viewport.
- Returning to the bottom or pressing `G` resumes the existing follow behavior.

No plan-specific scroll or selection code is introduced.

## Data Integrity

The collector already preserves every plan event in its history and queue. The
change is limited to presentation: `LoomTuiApp` must stop coalescing those
events. Trace persistence and event schemas remain unchanged.

The feed has no node-count cap. A scrollbar exposes the complete logical
timeline for the duration of the TUI session.

## Testing

### Plan History

- Feeding `plan.entered`, `plan.submitted`, multiple `plan.updated` events, and
  `plan.completed` creates the same number of feed nodes in the same order.
- Each node retains its original event type and revision.
- Earlier checklist snapshots are unchanged after later revisions arrive.
- Successful internal planning-tool events remain suppressed.
- Failed or rejected planning-tool events remain visible.

### Chronology

Replay a compact sequence equivalent to the yakDB trace:

```text
run.started
step.started
plan.entered
llm.requested
plan.submitted
tool event
plan.updated
tool event
plan.completed
run.completed
```

Assert that plan rows occur at their input positions instead of all collapsing
at the first plan position.

### Sticky Tail

- While following, a new plan revision follows the stream tail.
- While paused above the tail, a new plan revision preserves the current
  viewport and selected event.

### Regression

- Existing LLM stream aggregation tests continue to pass.
- Existing tool aggregation and planning-tool suppression tests continue to
  pass.
- The full TUI and repository test suites pass.

## Acceptance Criteria

- Every canonical plan lifecycle event has an independent timeline row.
- `Plan completed` appears at its actual completion position, not near the run
  start.
- Every plan row contains the complete checklist snapshot for that revision.
- No event-feed retention limit removes earlier logical nodes.
- LLM stream and tool execution aggregation remain unchanged.
- Sticky-tail scrolling remains unchanged.
