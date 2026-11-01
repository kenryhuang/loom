# Plan Transition Step Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End the current inner LLM/tool batch after every successful planning state mutation so the next outer step rebuilds the correct prompt and tool schema.

**Architecture:** Planning tools mark successful state-changing observations with a generic `controlFlow.stepBoundary` metadata contract. The generic LLM step loop recognizes that contract in both native and JSON-action paths, stops executing stale calls, and returns normally; `PlanningRuntime.wrap_loop` then persists and reprojects the new planning state on the next outer step.

**Tech Stack:** Python 3.11+, asyncio, pytest, Loom immutable core models

## Global Constraints

- Do not hard-code planning tool IDs in `loom.llm`.
- Preserve observation values returned to the model.
- Rejected transitions do not request a boundary.
- Stop later tool calls in the same response after the first requested boundary.
- Preserve normal multi-call ReAct behavior for observations without boundary metadata.
- A successful `finish` requests a boundary only when it completes an executing plan.

---

### Task 1: Planning Observation Boundary Contract

**Files:**
- Modify: `src/loom/runtime/planning.py:480-555`
- Test: `tests/runtime/test_planning.py:181-248`

**Interfaces:**
- Consumes: `Observation.metadata: Mapping[str, JsonValue] | None`.
- Produces: successful planning mutation observations with `metadata={"controlFlow": {"stepBoundary": True, "reason": "planning_transition"}}`.

- [ ] **Step 1: Add failing metadata contract tests**

Extend `test_plan_handlers_return_canonical_snapshots_and_rejections_are_recoverable`:

```python
assert entered.value.metadata["controlFlow"]["stepBoundary"] is True
assert submitted.value.metadata["controlFlow"]["stepBoundary"] is True
assert duplicate_submit.value.metadata is None
```

Extend the update-plan test:

```python
assert result.value.metadata == {
    "controlFlow": {"stepBoundary": True, "reason": "planning_transition"}
}
```

Extend the finish-gate test to assert a successful terminal-plan finish carries the same metadata, while a rejected incomplete finish returns `planning.guard` with no boundary metadata.

- [ ] **Step 2: Run tests and verify the contract is absent**

Run:

```bash
uv run pytest -q \
  tests/runtime/test_planning.py::test_plan_handlers_return_canonical_snapshots_and_rejections_are_recoverable \
  tests/runtime/test_planning.py::test_update_plan_schema_and_handler_allow_omitted_note \
  tests/runtime/test_planning.py::test_finish_gate_requires_terminal_plan_then_preserves_handler_result
```

Expected: assertions fail because current observations have no control-flow metadata.

- [ ] **Step 3: Add the planning boundary metadata helper**

In `planning.py`, add:

```python
_PLAN_TRANSITION_BOUNDARY = {
    "controlFlow": {
        "stepBoundary": True,
        "reason": "planning_transition",
    }
}


def _with_plan_transition_boundary(observation: Observation) -> Observation:
    metadata = dict(observation.metadata or {})
    metadata.update(_PLAN_TRANSITION_BOUNDARY)
    return replace(observation, metadata=metadata)
```

Pass `_PLAN_TRANSITION_BOUNDARY` as metadata when `_transition_result` creates its accepted observation. In `_guard_handler`, after a successful executing-plan `finish` and `controller.complete()`, replace the returned observation with `_with_plan_transition_boundary(result.value)`. Do not annotate guard rejections or ordinary inactive-mode `finish` calls.

- [ ] **Step 4: Run the planning suite**

Run:

```bash
uv run pytest -q tests/runtime/test_planning.py
```

Expected: all planning tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/loom/runtime/planning.py tests/runtime/test_planning.py
git commit -m "feat: mark plan transitions as step boundaries"
```

---

### Task 2: Honor Boundaries in Native and JSON Tool Loops

**Files:**
- Modify: `src/loom/llm/api.py:394-494`
- Test: `tests/llm/test_llm.py`

**Interfaces:**
- Consumes: `Observation.metadata["controlFlow"]["stepBoundary"] is True`.
- Produces: `_requests_step_boundary(observation: Observation) -> bool`; provider loop terminates normally after the first boundary observation.

- [ ] **Step 1: Add a failing native tool-call test**

Add a provider response containing `transition` followed by `stale_tool`; return a boundary observation for `transition`:

```python
def test_native_tool_boundary_ends_batch_and_skips_later_calls():
    async def scenario():
        provider = FakeProvider([
            ok(LlmResponse(
                content=None,
                tool_calls=(
                    LlmToolCall("call-transition", "transition", "{}"),
                    LlmToolCall("call-stale", "stale_tool", "{}"),
                ),
                finish_reason="tool_calls",
            ))
        ])
        calls = []

        async def call_tool(name, input_value, **options):
            calls.append(name)
            return ok(Observation(
                "transition-observation",
                name,
                {"accepted": True},
                NOW,
                metadata={"controlFlow": {"stepBoundary": True}},
            ))

        context = make_context(tools=(ToolRef("transition", "transition"), ToolRef("stale_tool", "stale")))
        result = await create_llm_step_function(provider)(context, make_runtime(call_tool=call_tool))

        assert result.ok
        assert calls == ["transition"]
        assert len(provider.messages) == 1

    asyncio.run(scenario())
```

- [ ] **Step 2: Add a failing JSON-action boundary test**

Use a JSON action target of `"transition, stale_tool"`, return boundary metadata for the first call, and assert only `transition` executes and the provider is called once.

- [ ] **Step 3: Run both tests and verify stale calls execute**

Run:

```bash
uv run pytest -q \
  tests/llm/test_llm.py::test_native_tool_boundary_ends_batch_and_skips_later_calls \
  tests/llm/test_llm.py::test_json_tool_boundary_ends_batch_and_skips_later_calls
```

Expected: failures show `stale_tool` executes or the provider is called again.

- [ ] **Step 4: Implement generic boundary detection**

Add:

```python
def _requests_step_boundary(observation: Observation) -> bool:
    metadata = observation.metadata
    if not isinstance(metadata, Mapping):
        return False
    control_flow = metadata.get("controlFlow")
    return isinstance(control_flow, Mapping) and control_flow.get("stepBoundary") is True
```

In each tool path, set a batch flag after appending the accepted observation. Break the per-response tool loop immediately, then break the enclosing provider `while True` before another request. Keep all existing behavior when the flag is false.

- [ ] **Step 5: Run LLM tests**

Run:

```bash
uv run pytest -q tests/llm/test_llm.py
```

Expected: all LLM tests pass, including existing multi-tool tests without boundary metadata.

- [ ] **Step 6: Commit**

```bash
git add src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "feat: stop llm batches at control boundaries"
```

---

### Task 3: Auto Plan Reprojection Regression

**Files:**
- Modify: `tests/tasks/test_task_runner.py`

**Interfaces:**
- Consumes: planning boundary metadata from Task 1 and LLM boundary handling from Task 2.
- Produces: an end-to-end regression covering phase-specific prompt and tool reprojection.

- [ ] **Step 1: Add an auto-plan provider and failing end-to-end test**

Create `AutoBoundaryPlanProvider` which records each tool set and follows this sequence across provider calls:

```text
1. enter_plan
2. submit_plan with Inspect and Verify
3. update_plan: Inspect in_progress
4. read_file
5. update_plan: Inspect completed, Verify in_progress
6. shell_execute
7. update_plan: both completed
8. finish
```

Parse generated step IDs from the current-plan lines in the system message after `submit_plan`. Add:

```python
def test_auto_plan_transitions_reproject_before_next_llm_request(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    provider = AutoBoundaryPlanProvider()
    sink = RecordingTraceSink()

    result = asyncio.run(run_generic_task(
        TaskRequest("Inspect and verify this project", workspace=tmp_path),
        provider=provider,
        options=TaskRunOptions(plan_mode=PlanMode.AUTO),
        trace_sink=sink,
    ))

    assert result.ok
    assert provider.tool_sets[0][-1] == "enter_plan"
    assert provider.tool_sets[1] == ("submit_plan",)
    assert "update_plan" in provider.tool_sets[2]
    guard_codes = [
        event["observation"].value.get("code")
        for event in sink.events
        if event["type"] == "observation.recorded"
        and event["observation"].source == "planning.guard"
    ]
    assert "PLAN_PHASE_INVALID" not in guard_codes
```

- [ ] **Step 2: Run the regression test**

Run:

```bash
uv run pytest -q tests/tasks/test_task_runner.py::test_auto_plan_transitions_reproject_before_next_llm_request
```

Expected before Tasks 1-2: the second request still contains stale normal tools. Expected after Tasks 1-2: pass.

- [ ] **Step 3: Reconcile existing forced-plan providers with boundary semantics**

Update fake providers that combine `update_plan` with a later normal tool in one response. Emit the transition and normal tool in successive responses, and obtain plan item IDs from the newly projected system prompt rather than an inner-loop tool message. Preserve their existing assertions about normal tool completion and plan revisions.

- [ ] **Step 4: Run task-runner and planning integration suites**

Run:

```bash
uv run pytest -q \
  tests/tasks/test_task_runner.py \
  tests/runtime/test_planning.py \
  tests/llm/test_llm.py \
  tests/integration/test_end_to_end_loom_flow.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run complete verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src/loom/runtime/planning.py src/loom/llm/api.py tests/runtime/test_planning.py tests/llm/test_llm.py tests/tasks/test_task_runner.py
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 6: Commit**

```bash
git add tests/tasks/test_task_runner.py
git commit -m "test: verify auto plan transition boundaries"
```
