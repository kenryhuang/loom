# Plan & Execute Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable Plan & Execute capability to the existing Loom ReAct loop, expose it through generic task CLI/config, and render its live checklist in the existing TUI event feed.

**Architecture:** Create a per-run planning controller in `loom.runtime` and use it to decorate a `MinimalLoopDefinition` plus its normal tool handlers. The decorator leaves `create_llm_step_function()` unchanged, synchronizes immutable plan snapshots through `Context.state.scratch`, emits full-snapshot plan events, and wraps `done` so planning phases cannot terminate early.

**Tech Stack:** Python 3.11, frozen dataclasses/enums, Loom runtime and trace sinks, `argparse`, Textual/Rich, pytest/pytest-asyncio, Ruff

---

## File Structure

- Create `src/loom/runtime/planning.py`: plan enums/models, controller, plan tools, handler gates, prompt/tool projection, loop decorator, and plan event emission.
- Modify `src/loom/runtime/__init__.py`: export the public planning API.
- Modify `src/loom/tasks/request.py`: store normalized `PlanMode` in `TaskRunOptions`.
- Modify `src/loom/tasks/config.py`: parse optional `run.plan_mode`.
- Modify `src/loom/tasks/cli.py`: add `--plan-mode` and apply CLI-over-config precedence.
- Modify `src/loom/tasks/runner.py`: assemble the planning runtime around the existing task context, loop, and tools.
- Modify `src/loom/tui/tui_app.py`: format plan nodes, coalesce lifecycle events by `plan_id`, and suppress successful plan-tool rows.
- Create `tests/runtime/test_planning.py`: controller, gates, wrapper, prompt, snapshot, and event tests.
- Modify `tests/tasks/test_task_config.py`: task config mode validation.
- Modify `tests/tasks/test_task_cli.py`: CLI parsing, defaults, and precedence.
- Modify `tests/tasks/test_task_runner.py`: scripted auto/force/off end-to-end ReAct integration and trace assertions.
- Modify `tests/unit/test_tui_app.py`: checklist formatting, node replacement, suppression, and rejection visibility.
- Modify `tests/unit/test_tui_collector.py`: raw plan event preservation.
- Modify `README.md`: document Plan & Execute modes and example invocations.

### Task 1: Plan Models and Controller

**Files:**
- Create: `src/loom/runtime/planning.py`
- Create: `tests/runtime/test_planning.py`

- [ ] **Step 1: Write failing controller transition tests**

Create `tests/runtime/test_planning.py` with deterministic IDs/timestamps and these first tests:

```python
from loom.runtime.planning import PlanController, PlanMode, PlanPhase


def _now():
    return "2026-07-31T00:00:00Z"


def _ids():
    values = iter(("plan_test", "step_1", "step_2", "step_3"))
    return lambda _prefix: next(values)


def _controller(mode=PlanMode.AUTO):
    return PlanController(mode, id_factory=_ids(), now=_now)


def test_controller_enters_and_submits_plan_with_stable_ids():
    controller = _controller()

    entered = controller.enter("The task has dependent steps")
    submitted = controller.submit("Inspect, implement, verify", ("Inspect loop", "Implement planning"))

    assert entered.ok
    assert submitted.ok
    assert submitted.value.phase is PlanPhase.EXECUTING
    assert submitted.value.revision == 1
    assert [(item.id, item.content, item.status.value) for item in submitted.value.items] == [
        ("step_1", "Inspect loop", "pending"),
        ("step_2", "Implement planning", "pending"),
    ]


def test_force_mode_starts_in_planning_and_queues_entered_event():
    controller = _controller(PlanMode.FORCE)

    assert controller.state.phase is PlanPhase.PLANNING
    events = controller.drain_events()
    assert [event.event_type for event in events] == ["plan.entered"]
    assert events[0].trigger == "cli_force"
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/runtime/test_planning.py -q
```

Expected: collection fails because `loom.runtime.planning` does not exist.

- [ ] **Step 3: Add immutable plan types and minimal transitions**

Implement these public types and signatures in `src/loom/runtime/planning.py`:

```python
class PlanMode(str, Enum):
    AUTO = "auto"
    FORCE = "force"
    OFF = "off"


class PlanPhase(str, Enum):
    INACTIVE = "inactive"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"


class PlanItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PlanItem:
    id: str
    content: str
    status: PlanItemStatus = PlanItemStatus.PENDING
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PlanState:
    plan_id: str | None
    phase: PlanPhase
    reason: str | None
    explanation: str | None
    revision: int
    items: tuple[PlanItem, ...]
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class PlanEvent:
    event_type: str
    trigger: str
    explanation: str | None
    plan: PlanState


```

Implement `PlanController(mode, *, id_factory, now)` with a read-only `state`
property and the public methods `enter(reason, *, trigger="llm")`,
`submit(explanation, items)`, and `drain_events()`. `OFF` and `AUTO` start
inactive; `FORCE` immediately enters planning and queues a `plan.entered` event
with trigger `cli_force`.

- [ ] **Step 4: Run the transition tests and verify GREEN**

Run `uv run pytest tests/runtime/test_planning.py -q`.

Expected: the two transition tests pass.

- [ ] **Step 5: Add failing update invariants and finish-gate tests**

Append tests that submit two items, then verify:

```python
def test_update_allows_future_replan_and_freezes_terminal_history():
    controller = _controller()
    controller.enter("complex")
    controller.submit("initial", ("Inspect", "Implement"))

    first = controller.update(
        "inspection started",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Implement", "status": "pending", "note": None},
        ),
    )
    second = controller.update(
        "inspection complete",
        (
            {"id": "step_1", "content": "Inspect", "status": "completed", "note": None},
            {"id": "step_2", "content": "Implement carefully", "status": "in_progress", "note": None},
            {"content": "Verify", "status": "pending", "note": None},
        ),
    )
    illegal = controller.update(
        "rewrite history",
        ({"id": "step_1", "content": "Different", "status": "pending", "note": None},),
    )

    assert first.ok and second.ok
    assert second.value.items[-1].id == "step_3"
    assert not illegal.ok
    assert illegal.error.code == "PLAN_TERMINAL_HISTORY_IMMUTABLE"


def test_finish_requires_all_items_terminal():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect",))

    assert not controller.complete().ok
    controller.update("done", ({"id": "step_1", "content": "Inspect", "status": "completed", "note": None},))
    assert controller.complete().ok
    assert controller.state.phase is PlanPhase.COMPLETED
```

Also add focused tests for duplicate IDs, two active items, missing skip reason, wrong-phase calls, and separate controller isolation.

- [ ] **Step 6: Run invariant tests and verify RED**

Run `uv run pytest tests/runtime/test_planning.py -q`.

Expected: failures identify missing `update()` and `complete()` behavior.

- [ ] **Step 7: Implement validation, update, completion, and canonical serialization**

Add `PlanController.update(explanation, items)`, `PlanController.complete()`,
`plan_state_dict(state)`, and `plan_state_from_mapping(value)` with the exact
argument and result shapes exercised by the tests above.

Use stable `PLAN_*` error codes, never mutate the previous frozen snapshot, never reuse assigned step IDs, freeze both completed and skipped history, and queue `plan.updated`/`plan.completed` only after successful transitions.

- [ ] **Step 8: Run planning core tests and commit**

Run:

```bash
uv run pytest tests/runtime/test_planning.py -q
uv run ruff check src/loom/runtime/planning.py tests/runtime/test_planning.py
git add src/loom/runtime/planning.py tests/runtime/test_planning.py
git commit -m "feat: add runtime plan controller"
```

Expected: all planning core tests and Ruff pass.

### Task 2: Planning Tools, Gates, and Loop Decorator

**Files:**
- Modify: `src/loom/runtime/planning.py`
- Modify: `src/loom/runtime/__init__.py`
- Modify: `tests/runtime/test_planning.py`

- [ ] **Step 1: Write failing plan-tool and recoverable-gate tests**

Add tests around `PlanningRuntime`:

```python
@pytest.mark.asyncio
async def test_guard_rejects_execution_without_active_item_as_observation():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect",))
    called = False

    async def normal_handler(_input, _options=None):
        nonlocal called
        called = True
        return ok(Observation("obs", "normal", {"ok": True}, _now()))

    guarded = planning.wrap_tools({"read_file": normal_handler, "finish": normal_handler})
    result = await guarded["read_file"]({}, {})

    assert result.ok
    assert called is False
    assert result.value.source == "planning.guard"
    assert result.value.value["accepted"] is False
    assert result.value.value["code"] == "PLAN_ACTIVE_ITEM_REQUIRED"
```

Add tests that `enter_plan`, `submit_plan`, and `update_plan` handlers return full canonical snapshots, accepted finish invokes the underlying finish handler, premature finish is recoverable, and successful/failed gates preserve business-tool semantics.

- [ ] **Step 2: Run focused tool tests and verify RED**

Run `uv run pytest tests/runtime/test_planning.py -q`.

Expected: failures identify the missing `PlanningRuntime` and handlers.

- [ ] **Step 3: Implement tool refs and handler wrapping**

Add the constant `PLAN_TOOL_IDS = frozenset({"enter_plan", "submit_plan",
"update_plan"})` and a public `PlanningRuntime` with:

- constructor arguments `mode`, `finish_tool_id="finish"`, `id_factory`, and
  `now`;
- a read-only `controller` property;
- `tool_refs()` returning phase-capable plan `ToolRef` values;
- `wrap_tools(handlers)` returning guarded normal handlers plus plan handlers;
  and
- `wrap_loop(definition)` returning a planning-aware `MinimalLoopDefinition`.

The three tool schemas must use `additionalProperties: false`;
`submit_plan.items` contains non-empty `content`; `update_plan.items` contains
optional `id`, content, enum status, and nullable note. Convert logical
controller errors into an `Observation` whose value contains `accepted: false`,
the controller error code/message, and `plan_state_dict(controller.state)`; do
not convert underlying normal-tool errors.

- [ ] **Step 4: Add failing wrapper tests for tools, prompt, scratch, done, and events**

Use fake `base_step` and `base_done` to assert:

```python
@pytest.mark.asyncio
async def test_planning_wrapper_persists_snapshot_emits_events_and_blocks_done():
    seen_tools = []
    emitted = RecordingSink()

    async def base_step(context, runtime):
        seen_tools.append(tuple(tool.id for tool in context.affordances.tools))
        await planning.wrap_tools({})["submit_plan"](
            {"explanation": "plan", "items": [{"content": "Inspect"}]}, {}
        )
        return ok(_step_result_for(context))

    definition = MinimalLoopDefinition(
        id="loop_test",
        version="v1",
        identity=IdentityLayer(role="test loop"),
        goal=GoalLayer(objective="test planning"),
        step=base_step,
        done=lambda _context, _runtime: ok(True),
    )
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    wrapped = planning.wrap_loop(definition)

    stepped = await wrapped.step(_context(), _runtime(emitted))
    done = await wrapped.done(stepped.value.context, _done_runtime())

    assert seen_tools == [("submit_plan",)]
    assert stepped.value.context.state.scratch["plan"]["phase"] == "executing"
    assert done.value is False
    assert [event["type"] for event in emitted.events] == ["plan.entered", "plan.submitted"]
```

Provide concrete test helpers in the test file for `Context`, `StepResult`, `Trace`, fake runtime sinks, and deterministic clocks rather than mocking core dataclasses.

- [ ] **Step 5: Run wrapper tests and verify RED**

Run `uv run pytest tests/runtime/test_planning.py -q`.

Expected: wrapper assertions fail until `wrap_loop` is implemented.

- [ ] **Step 6: Implement phase projections and loop wrapping**

Implement wrapper behavior:

```python
async def planned_step(context, runtime):
    controller.sync_from_context(context)
    planning.bind_step(runtime, context)
    await emit_pending_events(runtime, context, controller)
    prompt_context = project_context_for_plan(context, controller.state, normal_refs, plan_refs)
    try:
        result = await definition.step(prompt_context, runtime)
        if not result.ok:
            return result
        await emit_pending_events(runtime, context, controller)
        return ok(with_plan_snapshot(result.value, controller.state))
    finally:
        planning.unbind_step()


async def planned_done(context, runtime):
    state = plan_state_from_context(context)
    if state is not None and state.phase in {PlanPhase.PLANNING, PlanPhase.EXECUTING}:
        return ok(False)
    return await maybe_await(definition.done(context, runtime))
```

Use one replaceable runtime constraint ID for workflow instructions and recalculate visible tools on every outer step. `OFF` must return the original definition and original handlers unchanged.

Every async plan-tool handler must call `emit_pending_events` immediately after
a successful controller transition while the step runtime is bound. This is
what makes multiple `update_plan` calls inside one outer LLM step visible to the
TUI in real time; the post-step drain is only a safety net.

- [ ] **Step 7: Export API, run runtime tests, and commit**

Export `PlanController`, `PlanItem`, `PlanItemStatus`, `PlanMode`, `PlanPhase`, `PlanState`, `PlanningRuntime`, and `plan_state_dict` from `loom.runtime`.

Run:

```bash
uv run pytest tests/runtime/test_planning.py tests/runtime/test_runtime.py -q
uv run ruff check src/loom/runtime tests/runtime/test_planning.py
git add src/loom/runtime/planning.py src/loom/runtime/__init__.py tests/runtime/test_planning.py
git commit -m "feat: decorate loops with planning workflow"
```

### Task 3: Generic Task Runner Integration

**Files:**
- Modify: `src/loom/tasks/request.py`
- Modify: `src/loom/tasks/runner.py`
- Modify: `tests/tasks/test_task_runner.py`

- [ ] **Step 1: Write failing option/context and off-mode compatibility tests**

Add:

```python
def test_task_run_options_normalizes_plan_mode():
    assert TaskRunOptions().plan_mode is PlanMode.AUTO
    assert TaskRunOptions(plan_mode="force").plan_mode is PlanMode.FORCE


def test_make_task_context_adds_phase_appropriate_plan_tools(tmp_path):
    auto = make_task_context(TaskRequest("Audit", workspace=tmp_path), plan_mode=PlanMode.AUTO).unwrap()
    off = make_task_context(TaskRequest("Audit", workspace=tmp_path), plan_mode=PlanMode.OFF).unwrap()

    assert "enter_plan" in {tool.id for tool in auto.affordances.tools}
    assert not ({"enter_plan", "submit_plan", "update_plan"} & {tool.id for tool in off.affordances.tools})
```

Keep the existing normal tool assertions intact.

- [ ] **Step 2: Run focused tests and verify RED**

Run `uv run pytest tests/tasks/test_task_runner.py -q`.

Expected: `TaskRunOptions` and `make_task_context` reject the new plan-mode arguments.

- [ ] **Step 3: Wire PlanningRuntime into run_generic_task**

Add `plan_mode: PlanMode = PlanMode.AUTO` to `TaskRunOptions` and normalize strings in `__post_init__`.

In `run_generic_task` assemble in this order:

```python
planning = PlanningRuntime(run_options.plan_mode)
context = make_task_context(request, harness=task_harness, plan_mode=run_options.plan_mode, planning=planning)
normal_handlers = _filter_tool_handlers(make_task_tools(request), task_harness.allowed_tools)
definition = planning.wrap_loop(make_task_loop(request, provider, stream=run_options.stream, harness=task_harness))
handle = create(
    definition,
    registry=create_runtime_registry(tools=planning.wrap_tools(normal_handlers)),
    trace_store=JsonlTraceStore(run_options.trace_path) if run_options.trace_path is not None else None,
    event_policy=_task_trace_event_policy() if run_options.trace_path is not None else None,
)
```

Filter harness-selected normal tools before adding internal plan tools. In off mode the resulting definition/context/handler path must match the pre-feature behavior.

- [ ] **Step 4: Add a scripted force-mode integration test**

Create a fake provider that records advertised tool names and returns this sequence across outer phases:

```text
submit_plan(items=[Inspect, Verify])
final no-tool response
update_plan(step_1=in_progress) + read_file
update_plan(step_1=completed, step_2=in_progress) + shell_execute
update_plan(step_2=completed) + finish(report="done")
final no-tool response
```

Assert the result is `ok`, output is `done`, plan phase in final scratch is `completed`, the first tool set in force mode is exactly `submit_plan`, execution requests include normal tools plus `update_plan`, and `plan.entered/submitted/updated/completed` reach a recording trace sink.

- [ ] **Step 5: Add auto/off/recoverable-finish integration tests**

Add separate scripted providers proving:

- auto simple task ignores `enter_plan` and returns the existing finish report;
- off mode never advertises a plan tool;
- premature finish produces a tool observation with `accepted=false`, after which the provider updates the plan and completes normally; and
- a normal business-tool `Result.err` still fails the run exactly as before.

- [ ] **Step 6: Run task/runtime regressions and commit**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py tests/runtime/test_planning.py tests/runtime/test_runtime.py -q
uv run ruff check src/loom/tasks/request.py src/loom/tasks/runner.py tests/tasks/test_task_runner.py
git add src/loom/tasks/request.py src/loom/tasks/runner.py tests/tasks/test_task_runner.py
git commit -m "feat: integrate planning into generic task loop"
```

### Task 4: CLI and Configuration Modes

**Files:**
- Modify: `src/loom/tasks/config.py`
- Modify: `src/loom/tasks/cli.py`
- Modify: `tests/tasks/test_task_config.py`
- Modify: `tests/tasks/test_task_cli.py`

- [ ] **Step 1: Write failing config parsing tests**

Add TOML/YAML tests asserting `run.plan_mode: force` becomes `RunDefaults.plan_mode == "force"`, and `plan_mode: sometimes` returns `VALIDATION_FAILED` with metadata field `plan_mode`.

- [ ] **Step 2: Write failing CLI default and precedence tests**

Add:

```python
def test_parse_task_cli_defaults_plan_mode_to_auto(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parsed = parse_task_cli_args(["Summarize"])
    assert parsed.options.plan_mode is PlanMode.AUTO


def test_cli_plan_mode_overrides_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[run]\nplan_mode = "force"\n', encoding="utf-8")
    parsed = parse_task_cli_args(["Summarize", "--config", str(config), "--plan-mode", "off"])
    assert parsed.options.plan_mode is PlanMode.OFF
```

- [ ] **Step 3: Run CLI/config tests and verify RED**

Run `uv run pytest tests/tasks/test_task_config.py tests/tasks/test_task_cli.py -q`.

Expected: failures identify missing config field and CLI option.

- [ ] **Step 4: Implement config validation and CLI precedence**

Add `plan_mode: str | None = None` to `RunDefaults`; parse only `auto`, `force`, or `off`. Add:

```python
parser.add_argument("--plan-mode", choices=tuple(mode.value for mode in PlanMode))
```

Construct options with:

```python
plan_mode=PlanMode(args.plan_mode or run_defaults.plan_mode or PlanMode.AUTO.value)
```

- [ ] **Step 5: Run CLI/config tests, help smoke tests, and commit**

Run:

```bash
uv run pytest tests/tasks/test_task_config.py tests/tasks/test_task_cli.py -q
uv run loom task --help | rg -- "--plan-mode"
uv run python -m loom.tasks.run --help | rg -- "--plan-mode"
git add src/loom/tasks/config.py src/loom/tasks/cli.py tests/tasks/test_task_config.py tests/tasks/test_task_cli.py
git commit -m "feat: expose task plan mode controls"
```

### Task 5: JSONL Plan Event Verification

**Files:**
- Modify: `tests/tasks/test_task_runner.py`
- Modify: `tests/observability/test_trace_persistence.py`

- [ ] **Step 1: Write a failing JSONL full-snapshot event test**

Run the scripted force-mode task with `trace_path`, parse every JSONL record, and assert:

```python
plan_records = [record for record in records if record.get("eventType", "").startswith("plan.")]
assert [record["eventType"] for record in plan_records] == [
    "plan.entered", "plan.submitted", "plan.updated", "plan.updated", "plan.completed"
]
assert all(record["payload"]["plan"]["plan_id"] == "plan_test" for record in plan_records)
assert all("items" in record["payload"]["plan"] for record in plan_records)
```

- [ ] **Step 2: Run the JSONL test and verify RED if event payloads are incomplete**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py::test_force_plan_events_are_persisted_as_full_snapshots -q
```

- [ ] **Step 3: Correct event payload/recording integration and verify GREEN**

Ensure plan events use the existing runtime sink and are not included in `STREAM_DELTA_TRACE_EVENTS`. Do not add a second trace writer.

- [ ] **Step 4: Run observability/task tests and commit**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py tests/observability/test_trace_persistence.py -q
git add src/loom/runtime/planning.py tests/tasks/test_task_runner.py tests/observability/test_trace_persistence.py
git commit -m "test: verify persisted plan lifecycle events"
```

### Task 6: TUI Checklist Node

**Files:**
- Modify: `src/loom/tui/tui_app.py`
- Modify: `tests/unit/test_tui_app.py`
- Modify: `tests/unit/test_tui_collector.py`

- [ ] **Step 1: Write failing formatting tests**

Create a `plan.updated` `TuiEvent` with completed, in-progress, pending, and skipped items and assert:

```python
line = str(_format_event_line(event))
detail = _format_event_detail_plain(event)

assert "Plan" in line
assert "executing" in line
assert "3/5 terminal" in line
assert "✓ step_1" in detail
assert "→ step_3" in detail
assert "○ step_4" in detail
assert "⊘ step_5" in detail
assert "not required" in detail
```

- [ ] **Step 2: Write failing coalescing and suppression tests**

In a Textual `run_test()` session, feed `plan.entered`, `plan.submitted`, and `plan.updated` with the same `plan_id`. Assert `feed.event_count == 1` and the event at index 0 is the latest revision. Feed successful `tool.started/tool.completed` for `update_plan` and assert the count does not change. Feed a completed plan-tool result with `accepted=false` and assert it remains visible.

- [ ] **Step 3: Write collector history preservation test**

Emit three plan events through `TuiEventCollector` and assert `collector.event_count == 3`, event order is unchanged, and the queue yields all three.

- [ ] **Step 4: Run TUI tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_tui_app.py tests/unit/test_tui_collector.py -q
```

- [ ] **Step 5: Implement plan formatting and in-place event handling**

Add `PLAN_PRESENTATION_EVENTS`, `_plan_event_parts`, and `_append_plan_detail`. Extend `_event_scope`, `_event_status`, `_event_marker_color`, `_event_conversation_parts`, and `_format_event_detail` for `plan.*`.

In `LoomTuiApp.__init__` add:

```python
self._plan_event_indices: dict[str, int] = {}
self._pending_plan_tool_executions: dict[str, _ToolExecutionState] = {}
```

Handle plan events before generic tool events. Create on first `plan_id`, otherwise call `feed.update_event(index, event)`. Suppress successful plan tool executions only after their output reports `accepted` other than false; preserve rejected calls as normal tool nodes.

Do not mount a plan tool's `tool.started` event immediately. Aggregate it in
`_pending_plan_tool_executions`; on `tool.completed`, discard the aggregate for
an accepted result or mount the completed aggregate when `accepted` is false.
This avoids briefly showing a row that must later disappear.

- [ ] **Step 6: Run all generic and optimize TUI tests and commit**

Run:

```bash
uv run pytest tests/unit/test_tui_app.py tests/unit/test_tui_collector.py tests/optimize/test_tui_app.py tests/optimize/test_tui_runner.py -q
uv run ruff check src/loom/tui/tui_app.py tests/unit/test_tui_app.py tests/unit/test_tui_collector.py
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py tests/unit/test_tui_collector.py
git commit -m "feat: render live plan checklist in TUI"
```

### Task 7: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document Plan & Execute usage**

Add a Generic Task Runner subsection containing:

```bash
uv run loom task "Audit and improve this project" --plan-mode auto --tui
uv run loom task "Exercise the planning workflow" --plan-mode force --tui
uv run loom task "Run the legacy ReAct path" --plan-mode off
```

Explain that auto may enter planning after exploration, force begins with plan submission, off exposes no plan tools, and traces contain `plan.*` snapshots.

- [ ] **Step 2: Run the complete verification suite**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run loom task --help
uv run python -m loom.tasks.run --help
git diff --check
```

Expected: zero test failures, zero Ruff errors, both help commands list `--plan-mode {auto,force,off}`, and no whitespace errors.

- [ ] **Step 3: Review scope and compatibility**

Inspect `git diff` from the plan base and verify:

- `create_llm_step_function()` has no planning branches;
- off mode omits plan tools/state/events and existing task tests still pass;
- each plan event carries a full snapshot;
- the collector retains all plan events while the TUI coalesces only presentation;
- no nested planner/executor loop, plan side panel, resume system, or unrelated refactor was added.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: explain task planning modes"
```
