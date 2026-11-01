# Plan Transition Step Boundary Design

## Status

Approved after analysis of `runs/yakdb-incremental-index-auto.jsonl`.

## Problem

The LLM tool loop builds its system prompt and tool schema once at the start of
an outer runtime step. In auto mode, a successful `enter_plan` changes the
planning phase from `inactive` to `planning`, but the same inner tool loop keeps
using the old prompt and tool schema. The model therefore continues to see
normal tools and `enter_plan`, while `submit_plan` is unavailable. Runtime
guards reject those stale calls until the model eventually returns without a
tool call and allows the next outer step to reproject the context.

The same stale-context window exists after `submit_plan` and `update_plan`.

## Goal

Treat every successful plan-state mutation as a generic control-flow boundary.
The current inner LLM/tool batch must end immediately so the next outer runtime
step rebuilds the prompt, visible tools, and persisted plan snapshot.

## Control Contract

Successful `enter_plan`, `submit_plan`, and `update_plan` observations carry
internal metadata. A successful `finish` carries the same metadata only when it
completes an executing plan:

```python
{"controlFlow": {"stepBoundary": True, "reason": "planning_transition"}}
```

The contract is generic: the LLM loop reacts to the metadata and does not know
planning tool IDs. Observation values returned to the model remain unchanged.
Rejected plan transitions do not carry the boundary signal because they do not
change runtime state.

## LLM Loop Behavior

Both native tool calls and JSON-action tool calls inspect each successful tool
observation. When an observation requests a step boundary:

1. Keep the transition observation and emitted plan event.
2. Stop executing later tool calls from the same assistant response.
3. Do not make another provider request in the current inner loop.
4. Complete the current trace and return its `StepResult` normally.
5. Let `PlanningRuntime.wrap_loop` persist the plan snapshot.
6. On the next outer step, rebuild the system constraint and tool schema from
   the new phase.

For example:

```text
inactive tools: normal tools + enter_plan
  -> enter_plan succeeds and requests a step boundary
planning tools: submit_plan only
  -> submit_plan succeeds and requests a step boundary
executing tools: normal tools + update_plan
  -> terminal-plan finish succeeds and requests a step boundary
```

If a response contains a transition followed by other tool calls, calls after
the transition are not executed. They were selected against a stale phase and
must be reconsidered by the model in the next outer step.

Normal tools do not request a boundary and retain the current multi-call ReAct
behavior.

## Error Handling

- A failed runtime/tool result retains existing failure behavior.
- A planning guard rejection is recoverable but does not itself create a step
  boundary.
- The boundary is honored only after the transition observation has been
  recorded successfully.

## Testing

- Planning unit tests assert accepted transitions carry the metadata and
  rejected transitions do not. They also assert that only a `finish` which
  completes an executing plan carries the boundary.
- Native tool-loop tests assert a boundary stops the provider loop and skips
  later calls in the same response.
- JSON-action tests assert the same behavior.
- An auto-mode task-runner regression asserts:
  - first request exposes normal tools plus `enter_plan`;
  - after `enter_plan`, the next request exposes only `submit_plan`;
  - after `submit_plan`, the next request exposes normal tools plus
    `update_plan`;
  - no `PLAN_PHASE_INVALID` observation occurs.

## Non-Goals

- Do not infer phase changes from tool names in the LLM module.
- Do not refresh schemas midway through the same provider conversation.
- Do not change plan validation, checklist semantics, or normal tool batching.
- Do not solve unrelated shell argument formatting failures from the analyzed
  run.
