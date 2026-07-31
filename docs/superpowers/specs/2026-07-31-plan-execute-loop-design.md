# Plan & Execute Loop Design

## Goal

Add an optional Plan & Execute workflow to Loom's existing generic ReAct task
loop without replacing the loop or duplicating its LLM/tool execution path.
The LLM may enter planning after any amount of exploration, while callers may
force or disable planning for deterministic testing and regression coverage.

## Scope

This design introduces a reusable runtime planning capability and integrates it
with `run_generic_task`. It includes:

- a runtime-owned plan state machine;
- `enter_plan`, `submit_plan`, and `update_plan` tools;
- strict plan execution and finish gates;
- `auto`, `force`, and `off` CLI/config modes;
- plan lifecycle trace events; and
- an updating checklist node in the existing TUI event feed.

It does not introduce a separate planner or executor loop, nested plans,
parallel active plan items, persistence/resume for task runs, or a dedicated TUI
side panel.

## Chosen Architecture

Planning is a reusable capability that decorates a `MinimalLoopDefinition`.
The existing `create_llm_step_function()` remains responsible for the ReAct
conversation and tool-call loop. A planning wrapper changes the visible tools
and workflow instructions at outer step boundaries, reduces plan state, emits
domain events, and changes completion behavior while planning is active.

This is preferred over adding workflow-specific branches directly to the LLM
step or creating nested planner/executor loops. It keeps the LLM step focused,
preserves existing messages and traces, and allows other Loom loops to opt into
the same capability later.

The runtime module exposes a per-run planning assembly with responsibilities
equivalent to:

```text
PlanningRuntime
├── controller: PlanController
├── tool_refs(): plan tool definitions
├── wrap_tools(normal_handlers): guarded normal handlers + plan handlers
└── wrap_loop(definition): planning-aware step and done functions
```

No planning state is global. Every run receives its own controller.

## Data Model

### PlanMode

Configuration policy with three values:

- `auto`: start in normal ReAct mode and expose `enter_plan`;
- `force`: start in planning mode before the first LLM request; and
- `off`: omit all planning behavior and retain the existing ReAct path.

### PlanPhase

Runtime state with four values:

- `inactive`: no plan has been requested;
- `planning`: the next workflow action is submission of a plan;
- `executing`: a submitted plan is being executed; and
- `completed`: all plan items are terminal and `finish` has been accepted.

### PlanItem

Each flat checklist item contains:

- a stable runtime-assigned ID such as `step_1`;
- non-empty content;
- a status: `pending`, `in_progress`, `completed`, or `skipped`; and
- an optional note, required when the status is `skipped`.

### PlanState

The complete immutable snapshot contains:

- `plan_id`;
- `phase`;
- entry `reason`;
- latest `explanation`;
- monotonically increasing `revision`;
- ordered plan items; and
- creation and update timestamps.

The current snapshot is stored in `Context.state.scratch["plan"]` after each
outer step. This makes the plan visible in immutable context snapshots and
traces. The controller keeps the live per-run view required to validate
multiple sequential tool calls within one LLM step. The planning wrapper keeps
the controller and context snapshot synchronized at every outer step boundary.

## State Machine

```text
off
└── unchanged ReAct loop; no plan tools

auto
inactive
├── normal ReAct tools
└── enter_plan
      ↓
planning
└── submit_plan only
      ↓
executing
├── normal guarded tools
├── update_plan
└── guarded finish
      ↓
completed

force
planning (entered before the first LLM request)
└── same flow as auto after entry
```

In `auto`, the LLM may call `enter_plan` after any number of exploratory ReAct
rounds. A plan may be entered only once per run.

The current LLM step implementation fixes the visible tool list for all inner
LLM/tool rounds within one outer runtime step. Consequently, a phase-changing
plan call must be the last tool action in that outer step. The controller gates
subsequent tool calls made against the stale advertised list and returns a
recoverable rejection. The next outer step advertises the correct tool set for
the new phase.

## Loop Decoration

Planning wraps both `step` and `done`.

The step wrapper:

1. synchronizes the controller from the input context;
2. binds the current step runtime and trace coordinates to the per-run planning
   assembly;
3. emits queued events, including forced entry before the first LLM request;
4. derives phase-specific tool references and workflow instructions;
5. invokes the original step, while each successful plan transition flushes its
   newly queued domain event immediately through the bound runtime sink;
6. drains any remaining events and unbinds the step runtime;
7. writes the canonical plan snapshot into `StateLayer.scratch`; and
8. returns a normal `StepResult` with the original trace/output semantics.

The done wrapper behaves as follows:

- `off` or `inactive`: delegate to the original `done` predicate;
- `planning` or `executing`: return false; and
- `completed`: delegate to the original `done` predicate.

Wrapping `done` is required because the current generic task loop considers a
context with a decision complete. Phase-changing plan calls deliberately end an
outer step, but must not terminate the task.

## Tool Protocol

### enter_plan

Input:

```json
{"reason":"Why this task benefits from explicit planning"}
```

It is available only in `auto + inactive`. It transitions the controller to
`planning` and returns the canonical state.

### submit_plan

Input:

```json
{
  "explanation":"Summary of the approach",
  "items":[
    {"content":"Inspect the current loop"},
    {"content":"Integrate planning state and tools"}
  ]
}
```

It is available only in `planning`. At least one non-empty item is required.
The runtime assigns stable IDs, initializes every item as `pending`, increments
the revision, and transitions to `executing`.

### update_plan

Input is a complete replacement snapshot of the ordered items:

```json
{
  "explanation":"Inspection finished; begin implementation",
  "items":[
    {"id":"step_1","content":"Inspect the current loop","status":"completed","note":null},
    {"id":"step_2","content":"Integrate planning state and tools","status":"in_progress","note":null},
    {"content":"Add TUI checklist tests","status":"pending","note":null}
  ]
}
```

Existing items must supply their IDs. New items omit the ID and receive one
from the runtime. A successful update increments the revision and returns the
fully normalized canonical state.

The controller enforces these invariants:

- item IDs are unique;
- no more than one item is `in_progress`;
- terminal items (`completed` or `skipped`) cannot be removed, reordered,
  renamed, or moved out of their terminal status;
- skipped items require a non-empty reason;
- pending and in-progress items may be edited, reordered, removed, or skipped;
  and
- new items receive IDs that are never reused within the plan.

## Execution Gates

While the phase is `executing`:

- a normal execution tool requires exactly one `in_progress` item;
- multiple normal tool calls may execute under the same active item;
- `update_plan` completes or skips the current item and selects the next item;
- `finish` is rejected until every item is `completed` or `skipped`; and
- accepted `finish` changes the plan phase to `completed`.

The normal non-planning ReAct path may still call `finish` directly. In `off`,
normal tool handlers are not wrapped with plan gates.

## Prompt Behavior

The wrapper injects a replaceable runtime workflow instruction into the context
used by the LLM step:

- `inactive`: explain that exploration is allowed and `enter_plan` should be
  used when the task has multiple dependent steps, meaningful uncertainty, or
  requires coordinated changes;
- `planning`: require a single structured `submit_plan` transition and no
  execution work;
- `executing`: include the complete current checklist, active item, transition
  rules, and finish gate; and
- `completed`: allow the final response to be produced.

The current plan is injected every outer step rather than relying on recent
observation history, which may be truncated by `max_history_steps`.

## CLI and Configuration

The generic task CLI accepts:

```bash
loom task "..." --plan-mode auto
loom task "..." --plan-mode force
loom task "..." --plan-mode off
```

`TaskRunOptions` stores a normalized `PlanMode`. CLI values override
`run.plan_mode` from task configuration; the final default is `auto`.

`force` initializes the controller in `planning` and queues `plan.entered` with
trigger `cli_force`. `off` does not add plan tool refs, handlers, prompt
instructions, state, or events.

Both `loom task` and `python -m loom.tasks.run` continue to use the same parser
and runner behavior.

## Events

Every legal lifecycle transition emits one of:

- `plan.entered`;
- `plan.submitted`;
- `plan.updated`; or
- `plan.completed`.

Every event contains:

- `run_id`, `loop_id`, `trace_id`, and `step_number`;
- `plan_id` and `revision`;
- trigger (`llm` or `cli_force`);
- latest explanation;
- the complete canonical `PlanState`; and
- event timestamp.

The controller queues domain events. During a step, the wrapper binds the
current runtime sink and trace coordinates; each async plan-tool handler flushes
its successful transition immediately, so TUI progress updates do not wait for
the outer LLM step to finish. The wrapper emits forced entry before the first
LLM request and drains any leftovers when the step returns. Full snapshots make
each event independently renderable and avoid requiring consumers to replay
patches. Existing JSONL trace policy records these non-streaming events.

## Recoverable Protocol Rejections

Logical protocol errors must not terminate an otherwise recoverable LLM run.
Examples include:

- calling a normal tool without an active plan item;
- calling `finish` before all items are terminal;
- calling a phase-changing tool in the wrong phase;
- submitting an invalid plan transition; or
- trying to modify completed history.

These calls return an observation containing `accepted: false`, a stable error
code, a human-readable reason, and the current canonical plan state. The LLM can
correct the call in the same ReAct step.

Malformed tool arguments that cannot be parsed, corrupted internal state,
event-sink failures, cancellations, and existing business-tool failures retain
their current fatal or tool-specific semantics.

## TUI Behavior

The trace stream retains all plan lifecycle events. `TuiEventCollector` keeps
the complete event history and continues to enqueue every normalized event.

`LoomTuiApp` maintains a mapping from `plan_id` to an event-feed index:

- `plan.entered` creates the plan node;
- subsequent plan events replace that node in place; and
- `plan.completed` leaves the final completed checklist visible.

The node title shows phase, revision, and terminal progress. Its expanded detail
shows the entry reason, latest explanation, every item, and item notes.

```text
Plan · executing · revision 3 · 3/5 terminal

✓ step_1  Inspect current loop
✓ step_2  Define planning state
→ step_3  Integrate plan tools
○ step_4  Add TUI rendering
⊘ step_5  Legacy migration — skipped: not required
```

Markers are `○ pending`, `→ in_progress`, `✓ completed`, and `⊘ skipped`.

Successful `enter_plan`, `submit_plan`, and `update_plan` generic tool rows are
suppressed from the visual feed because the plan node contains the same
information. Their raw tool and plan events remain in traces and collector
history. Rejected plan/guard calls remain visible as error/tool nodes with the
rejection reason.

No dedicated plan side panel is introduced.

## Testing Strategy

### Planning Core

Unit tests cover all legal transitions, wrong-phase rejection, stable item IDs,
revision increments, terminal-history immutability, editable future items,
single-active-item validation, skipped reasons, finish gating, and isolation
between controllers.

### Loop Wrapper

Tests with fake step and done functions cover:

- exact pass-through behavior in `off`;
- entry from any `auto` outer step;
- forced entry before the first LLM request;
- phase-aware tool visibility and prompt content;
- suppression of premature loop completion;
- plan snapshots in context scratch; and
- event order and full-snapshot payloads.

### Generic Task Integration

A scripted fake provider exercises:

```text
enter_plan
→ submit_plan
→ update_plan(step_1=in_progress) + normal tool
→ update_plan(step_1=completed, step_2=in_progress)
→ normal tool
→ update_plan(all completed)
→ finish
```

Additional tests cover a simple auto-mode task that never plans, an off-mode
task with no plan tools, recoverable premature finish, and both CLI entry points.

### CLI and Configuration

Tests cover all accepted modes, invalid mode rejection, CLI precedence over
config, and the `auto` default.

### TUI

Tests verify that the collector preserves every event, the app creates one node
per plan, updates it in place, renders all markers and progress correctly,
suppresses successful plan-tool rows, preserves rejected calls, and does not
regress non-plan event handling.

### Verification

The implementation must pass the full pytest suite, Ruff, JSONL event checks,
and this manual acceptance path:

```bash
uv run loom task "A complex multi-step task" --plan-mode force --tui
```

The first LLM phase must show a Plan node; plan updates must refresh the same
checklist; incomplete plans must not finish; and a terminal plan must allow the
normal final report.

## Compatibility

- Existing ReAct mechanics and LLM/tool message transcripts remain the only
  execution engine.
- Existing task tools retain their behavior outside active plan gates.
- `off` provides an explicit legacy-compatible path.
- Existing loops do not acquire planning unless they opt into the decorator.
- Existing TUI event rendering remains unchanged for non-plan events.
