# Auto Plan Workflow Routing Design

## Status

Approved in conversation on 2026-08-02.

## Context

Loom currently treats `PlanMode.AUTO` as an inactive plan controller whose
normal projection exposes every task tool plus `enter_plan`. The workflow
constraint asks the task model to call `enter_plan` when the task has multiple
dependent steps, but using any normal tool implicitly starts ReAct without
recording that choice. In the analyzed `yakdb-incremental-index-auto.jsonl`
traces, both auto runs chose ordinary tools and never reconsidered planning,
while the equivalent forced run completed through the plan lifecycle.

The existing plan-transition step-boundary mechanism is correct once a plan
tool is selected. The missing part is an explicit, observable decision path for
selecting ReAct or Plan, plus bounded opportunities to reconsider that choice
when execution reveals additional complexity.

## Goals

- Give `auto` mode an explicit initial workflow-routing decision.
- Allow the model to upgrade from ReAct to Plan at any time.
- Reconsider the route at bounded runtime checkpoints without letting runtime
  heuristics directly force Plan.
- Treat every route mutation as a control-flow boundary so prompts and tool
  schemas are never stale.
- Preserve the current `off` and `force` behavior.
- Persist route state in `Context` and emit enough trace data to explain every
  decision.
- Avoid premature task completion and repeated blocked-tool loops at route
  boundaries.

## Non-Goals

- Selecting a separate or cheaper routing model.
- Making runtime heuristics decide that a task must use Plan.
- Implementing context compaction as part of this change.
- Changing plan checklist validation or progress semantics.
- Dynamically changing workflow inside an active Plan lifecycle.

## Chosen Approach

Use an explicit workflow router before normal auto-mode execution and at
bounded runtime checkpoints:

```text
auto
  -> undecided
      -> enter_plan     -> planning -> submit -> executing
      -> continue_react -> react
                            -> model calls enter_plan
                            -> runtime signal -> reviewing
                                                   -> enter_plan
                                                   -> continue_react
```

`force` enters planning immediately. `off` bypasses workflow routing. The task
model is reused for routing and receives a deliberately small tool surface.

## State Ownership

Plan checklist state and workflow-route state remain separate. `PlanState`
continues to own the Plan lifecycle. A new immutable `WorkflowRouteState` owns
only route selection and checkpoint counters and is stored in
`context.state.scratch["workflowRoute"]`.

The state contains at least:

```python
WorkflowRouteState(
    phase="undecided" | "react" | "reviewing" | "plan",
    revision=int,
    reason=str | None,
    trigger="initial" | "model" | "tool_failures" |
            "tool_budget" | "context_compaction" | None,
    review_count=int,
    calls_since_review=int,
    consecutive_failures=int,
    cooldown_remaining=int,
    task_execution_started=bool,
)
```

The exact serialization follows the existing `plan_state_dict` and
`plan_state_from_mapping` pattern. Missing route state is initialized according
to mode so older persisted contexts remain readable. An auto-mode context with
no task history starts `undecided`; an older auto-mode context that already has
task observations starts `react`; and a restored active plan implies route
`plan`. This prevents resume from repeating the initial router.

`task_execution_started` distinguishes a completed route transition from a
real task step. A successful `continue_react` leaves it false. The next step
sets it true immediately before invoking the normal task LLM. This prevents the
valid decision produced by the routing call from satisfying `_task_done` and
ending the task prematurely.

## Tool Projection

The controller projects tools from both route state and plan state:

| Mode or phase | Visible tools |
| --- | --- |
| `off` | Normal task tools |
| `force` / plan `planning` | `submit_plan` only |
| auto `undecided` | `enter_plan`, `continue_react` |
| auto `react` | Normal task tools plus `enter_plan` |
| auto `reviewing` | `enter_plan`, `continue_react` |
| plan `executing` | Normal task tools plus `update_plan` |
| plan `completed` | Existing completion projection |

`continue_react` records the route choice but does not create a plan.
`enter_plan` delegates to the existing `PlanController.enter` transition and
also changes the route to `plan`.

Routing calls send both routing tools to the task model and require one tool
call. Optional tool selection must not reduce this two-tool choice. The generic
LLM request surface should support a projected `tool_choice="required"` policy;
it must not express this using the current `required_tools` collection because
that collection means every listed tool remains pending rather than exactly
one of the alternatives being required.

## Initial Routing Prompt

The initial router receives:

- the objective, success criteria, and constraints;
- the workspace and available categories of task tools;
- the fact that no task execution has happened yet;
- an explicit rubric for choosing one routing tool.

The rubric recommends Plan for three or more dependent phases, investigation
followed by implementation and validation, changes spanning several modules or
artifacts, high uncertainty, or work where explicit progress tracking reduces
risk. It recommends ReAct for a one-shot query, read-only lookup, single local
edit, or a few independent actions.

Both tools require a concise `reason`. The model is not asked to write the
checklist during routing; checklist construction remains the next planning
step after `enter_plan`.

Routing requests use the normal provider event and token accounting paths.
They count against both the task token budget and outer-step budget, making the
extra auto-mode cost visible rather than hiding it in a side channel.

## Runtime Reconsideration

While and only while mode is `auto`, route is `react`, and Plan is inactive,
the runtime observes normalized tool outcomes. It requests a route review on
one of these signals:

- two consecutive recoverable tool failures;
- twelve normal tool calls since the last routing checkpoint;
- a context-compaction signal while the task is unfinished.

Context compaction is not currently present in the main task loop. This design
therefore defines an optional signal contract but does not make routing depend
on a compactor implementation.

Runtime signals never enter Plan directly. They change the route to
`reviewing`, attach a generic step-boundary directive, and allow the same task
model to choose `enter_plan` or `continue_react` on the next outer step. That
routing call additionally receives:

- a compact summary of completed work and observations;
- recent tool failures and their error codes;
- calls made since the prior checkpoint;
- the signal that requested review.

The model may call `enter_plan` directly from ordinary ReAct at any time. This
model-initiated path is not subject to runtime checkpoint limits.

## Checkpoint Limits

Defaults are centralized as named policy values:

- consecutive failure threshold: 2;
- tool-call evidence budget: 12;
- minimum normal calls between runtime reviews: 6;
- maximum runtime reviews per task: 3.

After `continue_react`, the controller resets consecutive failures and the
calls-since-review counter and starts the cooldown. Identical signals are
suppressed during cooldown. No route review occurs in `off`, `force`, planning,
executing, or completed phases.

These values are implementation defaults, not new CLI flags. Trace analysis
can justify later configuration without expanding the first version's public
surface.

## Loop Integration

The generic LLM loop remains unaware of planning types and tool IDs. After
each tool result has been normalized into an `Observation`, it invokes a
generic post-observation control hook supplied by the runtime wrapper. The hook
may return a control-flow directive such as:

```python
{"stepBoundary": True, "reason": "workflow_route_review"}
```

`PlanningRuntime` implements the workflow policy behind that hook. If the hook
requests review, the loop records the current observation, stops executing any
later tool calls from the same model response, completes the current trace,
and returns to the outer loop. The wrapper persists route state alongside plan
state. The next outer step reprojects the routing-only prompt and tool schema.

This hook runs after recoverable failures have been converted into failure
observations, so successful and failed calls share one accounting path. Fatal
tool errors keep their existing behavior.

Every successful `enter_plan` and `continue_react` is also a step boundary.
The done wrapper returns false for a route-transition step regardless of the
LLM decision recorded during that step. It also returns false while route phase
is `undecided` or `reviewing`, or Plan is `planning` or `executing`.

## Events and TUI

The route controller emits first-class append-only events:

- `workflow.routing.requested` with phase, trigger, counters, and review count;
- `workflow.route.selected` with selected route, reason, revision, trigger,
  and whether the decision was an in-flight upgrade.

Representative TUI rows are:

```text
Workflow  Evaluating complexity · initial
Workflow  ReAct · appears directly executable
Workflow  Reviewing · 2 consecutive tool failures
Workflow  Plan · investigation, implementation, and validation are dependent
Plan      submitted · 6 steps
```

Route reasons are truncated to one meaningful line in the TUI while complete
event payloads remain in JSONL. Routing events do not render a checklist. The
checklist begins only with a successful `plan.submitted` event. Historical
routing and plan events are appended to the timeline and are never replaced by
the latest state.

## Error Semantics

If a routing response contains no tool call or invalid arguments, retry once
with a stricter instruction. A second failure ends the run with
`WORKFLOW_ROUTE_FAILED`; it does not silently select ReAct.

Calls that conflict with current route state produce
`WORKFLOW_ROUTE_PHASE_INVALID`. Routing attempts are explicitly bounded, so an
invalid call cannot generate an indefinite sequence of recoverable blocked
observations. Rejected transitions do not mutate state or request a boundary.

## Test Strategy

Controller unit tests cover:

- mode initialization and route-state serialization;
- exact tool projection for every route and plan phase;
- valid and invalid route transitions;
- checkpoint counters, cooldown, and maximum reviews;
- migration from a context without `workflowRoute`.

LLM-loop tests cover:

- routing requests require exactly one of two alternative tools;
- a post-observation boundary stops later calls in the same response;
- recoverable failures reach the post-observation hook;
- route boundaries are plan-agnostic at the generic loop layer.

Task-runner integration tests cover:

- a simple auto task selects ReAct and then executes normal tools;
- a complex auto task selects Plan, submits a checklist, and executes it;
- `continue_react` cannot cause `_task_done` to terminate the run;
- direct model upgrade from ReAct enters planning;
- failures and tool-call budget initiate a review;
- `continue_react` resets counters and respects cooldown;
- active Plan phases never re-enter routing;
- invalid routing is retried once and then fails deterministically;
- `force` and `off` retain their current behavior;
- stale tools are skipped at every route or plan boundary;
- JSONL contains complete ordered workflow events;
- the TUI renders compact route summaries without replacing history.

## Compatibility and Rollout

The change affects only `PlanMode.AUTO`. `PlanMode.OFF` remains a direct ReAct
workflow, and `PlanMode.FORCE` still begins with `submit_plan`. Existing plan
state remains valid because workflow route state uses a separate scratch key.

The initial router adds one model call to every auto-mode task. Runtime router
calls are bounded to three and normally occur only when the initial ReAct
choice appears questionable based on new evidence.
