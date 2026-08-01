# LLM-Driven Plan Checkpoints Design

## Goal

Restore Plan & Execute to a dynamic workflow in which the LLM decides when a plan item is complete and records that semantic transition with `update_plan`. The runtime validates plan state but does not force a plan update after individual tool calls or LLM responses.

## Observed Failure

`PlanningRuntime` currently treats `llm_call_id` as an execution-batch identifier. After the first normal tool associated with one `llm_call_id`, the plan is marked dirty. A normal tool associated with the next `llm_call_id` is rejected with `PLAN_UPDATE_REQUIRED` until the model submits a complete plan snapshot.

This identifier does not represent a semantic unit of work. `create_llm_step_function()` assigns a new `llm_call_id` to every model response in its inner ReAct loop. The model used by the reported yakDB run generally emitted one tool call per response, so the runtime rule became “call `update_plan` after every tool.”

The affected run, `run_19fbbffb932_2_a2b44bb6`, contained:

- 12 successful normal-tool executions;
- 16 normal-tool calls rejected with `PLAN_UPDATE_REQUIRED`; and
- 13 accepted `update_plan` calls.

This behavior conflicts with the original Plan & Execute design, which allows multiple normal tools under one active item.

## Chosen Semantics

Planning checkpoints are driven by semantic plan transitions, not transport boundaries.

- Forced plan mode requires the model to create a plan before execution. It does not prescribe update frequency.
- Exactly one plan item must be `in_progress` before normal execution tools are used.
- While that item remains active, the model may issue any number of normal tools across any number of LLM responses.
- The model calls `update_plan` when its view of the workflow changes: an item completes or is skipped, the next item becomes active, a note changes materially, or future non-terminal work is revised.
- A handoff normally happens atomically in one complete-snapshot update: mark the current item terminal and the next item `in_progress`.
- The runtime never infers item completion from tool names, tool outputs, LLM-call counts, or elapsed time.
- `finish` remains rejected until every item is `completed` or `skipped`.

`update_plan` is therefore still the required protocol for recording a transition, but the decision to make that transition belongs to the LLM.

## Runtime Changes

Remove execution-batch barrier state from `PlanningRuntime`:

- `_execution_batch_id`;
- `_plan_update_required`; and
- `_llm_batch_id()`.

Normal-tool guards continue to enforce:

1. no execution tools while the plan is still being authored;
2. no execution tools after the plan is completed;
3. exactly one `in_progress` item during execution;
4. no `finish` until all items are terminal; and
5. no tools after an accepted `finish` completes the plan.

The existing `PlanController.update()` validation remains canonical. It continues to enforce stable IDs, a single active item, skipped-item notes, terminal-history immutability, and complete replacement snapshots.

`PLAN_UPDATE_REQUIRED` is removed from this execution path. Existing trace files containing that historical code remain readable.

## Prompt Changes

Replace batch-oriented workflow wording with semantic checkpoint guidance:

```text
Execute the active checklist item using as many normal tools as needed. Call
update_plan with the complete checklist snapshot only when item status, a
material note, or future work changes. When moving forward, mark the current
item completed or skipped and the next item in_progress in the same update.
Finish only after every item is terminal.
```

The prompt explicitly says not to update after each tool call. This reduces unnecessary full-checklist token usage and makes the expected behavior clear across providers that emit one or several tools per response.

## Tool Failure Behavior

A recoverable normal-tool failure remains available to the LLM as an observation. It does not force `update_plan`.

The LLM may:

- correct the tool arguments and retry;
- use a different tool;
- continue investigating;
- record a material finding in the active item note; or
- decide the item is blocked/completed and transition the plan.

Only the last two choices require `update_plan`, because they change plan state.

## TUI and Trace Behavior

No TUI schema or trace persistence changes are required.

- Accepted semantic updates continue to emit `plan.updated` snapshots and checklist history.
- Tools continue to show their concise outcome rows.
- Historical `PLAN_UPDATE_REQUIRED` observations remain renderable during trace replay.
- New runs no longer generate that guard code from normal-tool sequencing.

## Testing

Runtime tests will establish that:

- normal tools from different `llm_call_id` values execute while the same item remains active;
- several sequential and same-response tools are both allowed;
- a failed normal tool does not create an update barrier;
- an accepted `update_plan` can atomically complete one item and activate the next;
- normal tools are rejected without exactly one active item;
- `finish` remains rejected while any item is non-terminal;
- terminal item history remains immutable; and
- completed plans reject stale tools.

Workflow-description tests will assert semantic checkpoint language and reject batch-oriented or per-tool update wording.

An integration test will model a provider that emits one normal tool per LLM response. It must execute several tools without `PLAN_UPDATE_REQUIRED`, transition with an LLM-chosen `update_plan`, and finish successfully.

The full test suite and representative yakDB-like trace sequence must pass without changing non-planning ReAct behavior.

## Out of Scope

- Automatically deciding when an item is complete.
- Automatically generating or mutating plan notes.
- Adding time-based or tool-count-based checkpoint thresholds.
- Changing context compaction.
- Changing checklist presentation or TUI scrolling.
- Migrating historical trace events.
