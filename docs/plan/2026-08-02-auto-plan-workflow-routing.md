# Auto Plan Workflow Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `--plan auto` explicitly choose ReAct or Plan before execution and reconsider that choice at bounded runtime checkpoints.

**Architecture:** Add a workflow-route controller beside `PlanController`, persist both snapshots in context scratch, and let `PlanningRuntime` project the correct prompt and tool surface. Extend the generic LLM step with plan-agnostic request and observation policies so routing can require one tool call and runtime evidence can request a step boundary without teaching the LLM layer about Plan.

**Tech Stack:** Python 3.11+, immutable dataclasses, Loom `Context`/`Observation`/`Result`, pytest, Textual/Rich TUI, JSONL trace events.

## Global Constraints

- `PlanMode.OFF` remains direct ReAct and `PlanMode.FORCE` still begins with `submit_plan`.
- Auto routing reuses the configured task provider; no routing-model configuration is added.
- Runtime evidence requests a routing review but never directly forces Plan.
- Every successful route or plan transition is a step boundary.
- Route state is stored at `context.state.scratch["workflowRoute"]`; plan state remains at `context.state.scratch["plan"]`.
- Routing retries are bounded and must never create an indefinite blocked-tool loop.
- Context-compaction support is an optional signal contract; this plan does not implement compaction.
- Defaults are failure threshold 2, tool evidence budget 12, cooldown 6 calls, and maximum 3 runtime reviews.

---

### Task 1: Workflow Route State Machine

**Files:**
- Create: `src/loom/runtime/workflow_routing.py`
- Modify: `src/loom/runtime/__init__.py`
- Create: `tests/runtime/test_workflow_routing.py`

**Interfaces:**
- Produces: `WorkflowRoutePhase`, `WorkflowRoutePolicy`, `WorkflowRouteState`, `WorkflowRouteEvent`, `WorkflowRouteController`.
- Produces: `workflow_route_state_dict(state) -> dict[str, Any]` and `workflow_route_state_from_mapping(value) -> WorkflowRouteState`.
- Consumes: mode values `auto`, `force`, and `off` without importing `PlanController`, preventing a circular dependency.

- [ ] **Step 1: Write failing initialization and transition tests**

```python
def test_auto_route_starts_undecided_and_emits_initial_request():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    assert controller.state.phase is WorkflowRoutePhase.UNDECIDED
    assert [event.event_type for event in controller.drain_events()] == ["workflow.routing.requested"]


def test_continue_react_records_reason_and_requires_a_real_task_step():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    controller.drain_events()
    result = controller.select_react("One direct edit is sufficient")
    assert result.ok
    assert result.value.phase is WorkflowRoutePhase.REACT
    assert result.value.task_execution_started is False
    assert [event.event_type for event in controller.drain_events()] == ["workflow.route.selected"]


def test_force_route_starts_in_plan_without_initial_router():
    controller = WorkflowRouteController("force", now=lambda: NOW)
    assert controller.state.phase is WorkflowRoutePhase.PLAN
    assert controller.drain_events() == ()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -q tests/runtime/test_workflow_routing.py`

Expected: collection fails because `loom.runtime.workflow_routing` does not exist.

- [ ] **Step 3: Implement immutable route types and basic transitions**

```python
class WorkflowRoutePhase(StrEnum):
    UNDECIDED = "undecided"
    REACT = "react"
    REVIEWING = "reviewing"
    PLAN = "plan"


@dataclass(frozen=True, slots=True)
class WorkflowRoutePolicy:
    failure_threshold: int = 2
    tool_call_threshold: int = 12
    cooldown_calls: int = 6
    max_reviews: int = 3


@dataclass(frozen=True, slots=True)
class WorkflowRouteState:
    phase: WorkflowRoutePhase
    revision: int = 0
    reason: str | None = None
    trigger: str | None = None
    review_count: int = 0
    calls_since_review: int = 0
    consecutive_failures: int = 0
    cooldown_remaining: int = 0
    task_execution_started: bool = False
```

Implement `select_react`, `select_plan`, `mark_task_execution_started`,
`restore`, `drain_events`, and JSON-safe serialization. Empty reasons and
invalid phases return non-retryable Loom errors.

- [ ] **Step 4: Add failing checkpoint and restoration tests**

```python
def test_two_failures_request_one_bounded_review():
    controller = WorkflowRouteController("auto")
    controller.select_react("start directly")
    controller.mark_task_execution_started()
    assert controller.observe_tool(failed=True) is False
    assert controller.observe_tool(failed=True) is True
    assert controller.state.phase is WorkflowRoutePhase.REVIEWING
    assert controller.state.review_count == 1


def test_continue_after_review_resets_counts_and_starts_cooldown():
    controller = reviewing_controller()
    controller.select_react("continue with gathered evidence")
    assert controller.state.calls_since_review == 0
    assert controller.state.consecutive_failures == 0
    assert controller.state.cooldown_remaining == 6


def test_route_snapshot_round_trips_literal_values():
    restored = workflow_route_state_from_mapping(workflow_route_state_dict(reviewing_controller().state))
    assert restored == reviewing_controller().state
```

- [ ] **Step 5: Implement checkpoint accounting and limits**

`observe_tool(failed: bool) -> bool` increments call and failure counters,
decrements cooldown, and changes `react` to `reviewing` only when a threshold
is reached outside cooldown and below `max_reviews`. Add
`request_review(trigger: str, reason: str) -> Result` for the future
`context_compaction` signal.

- [ ] **Step 6: Run route-controller tests and verify GREEN**

Run: `pytest -q tests/runtime/test_workflow_routing.py`

Expected: all tests pass.

- [ ] **Step 7: Export the public route types and commit**

```bash
git add src/loom/runtime/workflow_routing.py src/loom/runtime/__init__.py tests/runtime/test_workflow_routing.py
git commit -m "feat: add workflow route state machine"
```

---

### Task 2: Generic LLM Request and Observation Policies

**Files:**
- Modify: `src/loom/llm/api.py`
- Modify: `src/loom/llm/__init__.py`
- Modify: `tests/llm/test_llm.py`

**Interfaces:**
- Produces: immutable `LlmStepPolicy` with `tool_choice`, `preserve_all_tools`, `require_tool_call`, `missing_tool_call_retries`, `invalid_tool_call_retries`, `retry_prompt`, and `failure_code`.
- Extends: `create_llm_step_function(..., step_policy_resolver=None, observation_policy=None)`.
- `step_policy_resolver(context) -> LlmStepPolicy | Result` is resolved once per outer step.
- `observation_policy(context, observation) -> Observation | Result` runs after success/failure normalization and before boundary inspection.

- [ ] **Step 1: Write failing tests for required alternative routing tools**

```python
def test_step_policy_requires_one_call_without_pending_every_tool():
    policy = LlmStepPolicy(tool_choice="required", preserve_all_tools=True, require_tool_call=True)
    provider = CapturingProvider([tool_response("continue_react", {"reason": "direct"})])
    result = asyncio.run(create_llm_step_function(provider, step_policy_resolver=lambda _context: policy)(route_context(), make_runtime()))
    assert result.ok
    assert provider.tool_choices == ["required"]
    assert provider.tool_sets == [("enter_plan", "continue_react")]
    assert provider.calls == 1
```

- [ ] **Step 2: Write failing tests for one retry and deterministic failure**

```python
def test_required_tool_call_retries_once_then_fails():
    policy = LlmStepPolicy(
        tool_choice="required",
        preserve_all_tools=True,
        require_tool_call=True,
        missing_tool_call_retries=1,
        retry_prompt="Call exactly one workflow routing tool.",
        failure_code="WORKFLOW_ROUTE_FAILED",
    )
    provider = FakeProvider([plain_response("no call"), plain_response("still no call")])
    result = asyncio.run(create_llm_step_function(provider, step_policy_resolver=lambda _context: policy)(route_context(), make_runtime()))
    assert not result.ok
    assert result.error.code == "WORKFLOW_ROUTE_FAILED"
    assert provider.calls == 2
```

- [ ] **Step 3: Run focused LLM tests and verify RED**

Run: `pytest -q tests/llm/test_llm.py -k 'step_policy or observation_policy'`

Expected: failures show `LlmStepPolicy` and the new parameters are absent.

- [ ] **Step 4: Implement `LlmStepPolicy` and request-policy resolution**

Resolve the policy before optional tool selection. When
`preserve_all_tools=True`, skip tool selection for that step. Use the policy's
`tool_choice` independently of `required_tools`, keeping the old
`required_tools` behavior unchanged for existing callers.

When a required call is absent, append the assistant response and the literal
retry prompt, then retry within the same token tracker. Exceeding the configured
retry count returns `failure_code` without producing a normal task decision.
When a required tool call contains malformed JSON arguments, append a matching
tool-result error for that call and apply `invalid_tool_call_retries`; exceeding
that count returns the same deterministic failure code.

- [ ] **Step 5: Write a failing post-observation boundary test**

```python
def test_observation_policy_sees_normalized_failure_and_can_end_batch():
    seen = []

    def policy(_context, observation):
        seen.append(observation.value)
        return replace(observation, metadata={"controlFlow": {"stepBoundary": True, "reason": "review"}})

    result = asyncio.run(create_llm_step_function(provider_with_two_calls(), observation_policy=policy)(context, failing_tool_runtime()))
    assert result.ok
    assert seen[0]["ok"] is False
    assert executed_tool_names == ["first"]
```

- [ ] **Step 6: Implement the async-capable observation policy in both tool paths**

Add one helper that accepts direct values, awaitables, and `Result`. Invoke it
for native tool calls and JSON actions after recoverable errors become
observations. Store the returned observation, then apply the existing
`_requests_step_boundary` check.

- [ ] **Step 7: Run LLM tests and verify GREEN**

Run: `pytest -q tests/llm/test_llm.py`

Expected: all LLM tests pass.

- [ ] **Step 8: Export the policy and commit**

```bash
git add src/loom/llm/api.py src/loom/llm/__init__.py tests/llm/test_llm.py
git commit -m "feat: add llm step control policies"
```

---

### Task 3: Initial Auto Routing and Safe Step Boundaries

**Files:**
- Modify: `src/loom/runtime/planning.py`
- Modify: `src/loom/tasks/runner.py`
- Modify: `tests/runtime/test_planning.py`
- Modify: `tests/tasks/test_task_runner.py`

**Interfaces:**
- Consumes: `WorkflowRouteController` and route serialization from Task 1.
- Consumes: `LlmStepPolicy`, `step_policy_resolver`, and `observation_policy` from Task 2.
- Produces: `PlanningRuntime.step_policy(context) -> LlmStepPolicy`.
- Produces: `PlanningRuntime.observe_tool(context, observation) -> Result[Observation]`.
- Adds: `continue_react` to the planning/runtime tool registry.

- [ ] **Step 1: Change context tests to require an explicit auto route**

```python
def test_make_task_context_auto_exposes_only_route_choice(tmp_path):
    context = make_task_context(TaskRequest("Audit", workspace=tmp_path), plan_mode=PlanMode.AUTO).unwrap()
    assert tuple(tool.id for tool in context.affordances.tools) == ("enter_plan", "continue_react")


def test_make_task_context_off_still_exposes_normal_tools(tmp_path):
    context = make_task_context(TaskRequest("Edit", workspace=tmp_path), plan_mode=PlanMode.OFF).unwrap()
    assert {tool.id for tool in context.affordances.tools} == {"read_file", "edit_file", "write_file", "shell_execute", "finish"}
```

- [ ] **Step 2: Add a failing integration test for ReAct routing not ending the task**

```python
def test_auto_continue_react_crosses_boundary_before_task_execution(tmp_path):
    provider = AutoReactTaskProvider()
    result = asyncio.run(run_generic_task(TaskRequest("Read and report", workspace=tmp_path), provider=provider))
    assert result.ok
    assert provider.tool_sets[0] == ("enter_plan", "continue_react")
    assert "read_file" in provider.tool_sets[1]
    assert provider.tool_choices[0] == "required"
    assert provider.calls >= 3
    assert result.value.run_result.context.state.scratch["workflowRoute"]["task_execution_started"] is True
```

The fake provider calls `continue_react`, then `read_file`, then `finish`; this
test specifically catches `_task_done` treating the route decision as final.

- [ ] **Step 3: Add a failing integration test for model-initiated upgrade**

```python
def test_auto_react_model_can_upgrade_to_plan_without_runtime_signal(tmp_path):
    provider = AutoUpgradePlanProvider()
    result = asyncio.run(run_generic_task(TaskRequest("Inspect then repair", workspace=tmp_path), provider=provider))
    assert result.ok
    assert provider.tool_sets[0] == ("enter_plan", "continue_react")
    assert "enter_plan" in provider.tool_sets[1]
    assert provider.tool_sets[2] == ("submit_plan",)
```

The provider selects ReAct initially, calls `enter_plan` from the first normal
task step, and submits a checklist only after the transition boundary.

- [ ] **Step 4: Run focused tests and verify RED**

Run: `pytest -q tests/tasks/test_task_runner.py -k 'auto_exposes_only_route_choice or auto_continue_react or model_can_upgrade'`

Expected: auto mode still exposes normal tools and the route boundary test fails.

- [ ] **Step 5: Add route tools, projection, prompt, snapshots, and events**

Update `PlanningRuntime` so auto `undecided`/`reviewing` exposes exactly the two
route tools. `continue_react` calls `select_react`; `enter_plan` first validates
both controllers, then moves route and plan state together. Successful route
tools return observations containing:

```python
metadata={"controlFlow": {"stepBoundary": True, "reason": "workflow_routing_transition"}}
```

Persist `workflowRoute` in `_with_plan_snapshot`, restore it in
`_sync_from_context`, and emit pending route events through the same trace sink
used by plan events.

The routing constraint includes the accepted rubric, current trigger and
counters, and a compact representation of recent observations already present
in the context. It explicitly says to call exactly one route tool.

- [ ] **Step 6: Wire generic policies through the task runner**

Extend `make_task_loop` with an optional `planning` argument and construct the
LLM step as:

```python
create_llm_step_function(
    provider,
    stream=stream,
    max_tool_calls_per_step=task_harness.max_tool_calls_per_step,
    prompt_options={"max_history_steps": task_harness.max_history_steps},
    step_policy_resolver=planning.step_policy if planning else None,
    observation_policy=planning.observe_tool if planning else None,
)
```

Pass the same `PlanningRuntime` instance from `run_generic_task` to
`make_task_context`, `make_task_loop`, and wrapped tools.

- [ ] **Step 7: Prevent route transitions from satisfying done**

The wrapped done function returns `False` while route phase is `undecided` or
`reviewing`, while `task_execution_started` is false, and during existing
planning/executing phases. Immediately before a normal ReAct task step,
`PlanningRuntime` marks execution started and persists that state in the
returned context.

- [ ] **Step 8: Update unrelated task-provider fixtures intentionally**

Tests concerned with file schemas, trace persistence, streaming, or individual
tools use `PlanMode.OFF`. Auto-specific fake providers explicitly answer the
route call before exercising task tools. Forced-plan fixtures remain unchanged.

- [ ] **Step 9: Run planning and task-runner tests and verify GREEN**

Run: `pytest -q tests/runtime/test_planning.py tests/tasks/test_task_runner.py`

Expected: all tests pass, including force/off compatibility and auto route boundaries.

- [ ] **Step 10: Commit initial routing integration**

```bash
git add src/loom/runtime/planning.py src/loom/tasks/runner.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py
git commit -m "feat: route auto tasks before execution"
```

---

### Task 4: Runtime Evidence Checkpoints and Bounded Route Failures

**Files:**
- Modify: `src/loom/runtime/planning.py`
- Modify: `src/loom/runtime/workflow_routing.py`
- Modify: `tests/runtime/test_planning.py`
- Modify: `tests/tasks/test_task_runner.py`

**Interfaces:**
- Consumes: `PlanningRuntime.observe_tool(context, observation)` from Task 3.
- Produces: optional `PlanningRuntime.request_route_review(trigger, reason) -> Result` entry point for context compaction.
- Produces: deterministic `WORKFLOW_ROUTE_FAILED` and `WORKFLOW_ROUTE_PHASE_INVALID` failures.

- [ ] **Step 1: Write a failing two-failure integration test**

```python
def test_auto_react_reviews_route_after_two_recoverable_failures(tmp_path):
    provider = FailureReviewProvider()
    result = asyncio.run(run_generic_task(TaskRequest("Repair the file", workspace=tmp_path), provider=provider))
    assert result.ok
    assert provider.tool_sets[0] == ("enter_plan", "continue_react")
    assert provider.tool_sets_at_review == ("enter_plan", "continue_react")
    route = result.value.run_result.context.state.scratch["workflowRoute"]
    assert route["review_count"] == 1
```

The provider selects ReAct, issues two failing edits, receives a routing-only
review step, and then enters Plan.

- [ ] **Step 2: Write failing tests for tool budget, cooldown, and review cap**

Use a small injected `WorkflowRoutePolicy` in unit tests so literal sequences
prove that a review happens exactly at the threshold, is suppressed during
cooldown, and cannot exceed `max_reviews`.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `pytest -q tests/runtime/test_planning.py tests/tasks/test_task_runner.py -k 'route_after or route_review or cooldown'`

Expected: observations do not yet trigger routing review boundaries.

- [ ] **Step 4: Apply route accounting only to normal task tools**

`PlanningRuntime.observe_tool` ignores `enter_plan`, `continue_react`,
`submit_plan`, and `update_plan`, and does nothing outside auto ReAct. For a
normal observation it derives failure from the normalized literal shape
`{"ok": false, "error": ...}` and delegates to `WorkflowRouteController`.
When review begins, merge this metadata into the observation without changing
its value:

```python
{"controlFlow": {"stepBoundary": True, "reason": "workflow_route_review"}}
```

- [ ] **Step 5: Bound invalid routing attempts**

Track invalid calls while phase is `undecided` or `reviewing`. The first
rejection is returned as a recoverable route observation with concise feedback;
the second returns a non-retryable `WORKFLOW_ROUTE_FAILED`. A valid route resets
the attempt count. A route tool invoked from an illegal phase returns
`WORKFLOW_ROUTE_PHASE_INVALID` and cannot mutate either controller.

- [ ] **Step 6: Add and test the optional external review signal**

```python
result = planning.request_route_review("context_compaction", "Context was compacted while unfinished")
assert result.ok
assert planning.route.state.phase is WorkflowRoutePhase.REVIEWING
```

The call is a no-op outside eligible auto ReAct state and obeys cooldown and
maximum-review limits.

- [ ] **Step 7: Run runtime and task integration tests and verify GREEN**

Run: `pytest -q tests/runtime/test_workflow_routing.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py`

Expected: all tests pass.

- [ ] **Step 8: Commit checkpoint behavior**

```bash
git add src/loom/runtime/workflow_routing.py src/loom/runtime/planning.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py
git commit -m "feat: reconsider auto routing from runtime evidence"
```

---

### Task 5: Workflow Trace and TUI Presentation

**Files:**
- Modify: `src/loom/tui/event_summary.py`
- Modify: `src/loom/tui/tui_app.py`
- Modify: `tests/unit/test_tui_event_summary.py`
- Modify: `tests/unit/test_tui_app.py`
- Modify: `tests/tasks/test_task_runner.py`

**Interfaces:**
- Consumes: `workflow.routing.requested` and `workflow.route.selected` payloads.
- Produces: compact `EventSummary` rows while preserving full event payloads in JSONL and expanded details.

- [ ] **Step 1: Write failing event-summary tests**

```python
def test_workflow_review_event_summarizes_trigger():
    event = _event("workflow.routing.requested", {"trigger": "tool_failures", "reason": "2 consecutive tool failures"})
    assert summarize_event(event) == EventSummary("Workflow reviewing", "2 consecutive tool failures", "orange")


def test_workflow_selection_summarizes_route_and_reason():
    event = _event("workflow.route.selected", {"route": "plan", "reason": "Investigation, implementation, and verification are dependent"})
    assert summarize_event(event) == EventSummary("Workflow Plan", "Investigation, implementation, and verification are dependent", "blue")
```

- [ ] **Step 2: Run TUI summary tests and verify RED**

Run: `pytest -q tests/unit/test_tui_event_summary.py -k workflow`

Expected: workflow events have no specialized summary.

- [ ] **Step 3: Implement workflow summaries and timeline classification**

Add `WORKFLOW_PRESENTATION_EVENTS`, map workflow scope/marker/status, and route
the two event types through `summarize_event`. Keep the reason on one truncated
line in `_format_event_line`; expanded detail shows trigger, revision, review
count, counters, and full reason. Do not send workflow events through the Plan
checklist renderer.

- [ ] **Step 4: Add a JSONL ordering assertion**

Extend the auto integration trace test to assert the literal order:

```python
assert workflow_types[:2] == ["workflow.routing.requested", "workflow.route.selected"]
route_index = event_types.index("workflow.route.selected")
next_task_tool_index = next(index for index in range(route_index + 1, len(event_types)) if event_types[index] == "tool.started")
assert route_index < next_task_tool_index
```

Assert every routing payload contains `revision`, `trigger`, and `reason`, while
plan events retain their complete checklist snapshots.

- [ ] **Step 5: Add a timeline-retention test**

```python
@pytest.mark.asyncio
async def test_workflow_timeline_keeps_initial_selection_and_later_review():
    app = LoomTuiApp(TuiEventCollector())
    async with app.run_test():
        app._handle_event(_workflow_event("workflow.routing.requested", trigger="initial"))
        app._handle_event(_workflow_event("workflow.route.selected", route="react", reason="direct"))
        app._handle_event(_workflow_event("workflow.routing.requested", trigger="tool_failures", reason="2 consecutive tool failures"))
        feed = app.query_one("#event_feed", EventFeedWidget)
        assert [event.event_type for event in feed.get_events()] == [
            "workflow.routing.requested",
            "workflow.route.selected",
            "workflow.routing.requested",
        ]
```

- [ ] **Step 6: Run TUI and integration tests and verify GREEN**

Run: `pytest -q tests/unit/test_tui_event_summary.py tests/unit/test_tui_app.py tests/tasks/test_task_runner.py`

Expected: all tests pass and route events remain distinct from Plan events.

- [ ] **Step 7: Commit trace presentation**

```bash
git add src/loom/tui/event_summary.py src/loom/tui/tui_app.py tests/unit/test_tui_event_summary.py tests/unit/test_tui_app.py tests/tasks/test_task_runner.py
git commit -m "feat: show workflow routing in task timeline"
```

---

### Task 6: Full Regression Verification

**Files:**
- Modify only if a failing regression exposes a defect in the files owned by Tasks 1-5.

**Interfaces:**
- Verifies all public and internal contracts introduced by the preceding tasks.

- [ ] **Step 1: Run formatting and static checks configured by the project**

Run: `uv run ruff check src tests`

Expected: exit code 0 with no diagnostics.

- [ ] **Step 2: Run the complete test suite**

Run: `uv run pytest -q`

Expected: exit code 0 and zero failed tests.

- [ ] **Step 3: Inspect the final diff against the approved spec**

Run:

```bash
git diff --check main...HEAD
git diff --stat main...HEAD
git log --oneline main..HEAD
```

Confirm every goal has a corresponding implementation and test, no secret
configuration is emitted, and no unrelated files changed.

- [ ] **Step 4: Return any verification failure to its owning TDD task**

Do not paper over a failed final command. Reproduce the failure with the
narrowest test from Tasks 1-5, observe RED, make the minimal correction, rerun
that task's focused suite, and then repeat Steps 1-3 above.
