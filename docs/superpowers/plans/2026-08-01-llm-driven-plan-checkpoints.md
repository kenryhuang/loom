# LLM-Driven Plan Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LLM execute multiple tools under one active plan item and call `update_plan` only when semantic plan state changes.

**Architecture:** Remove transport-level batch state and `llm_call_id` enforcement from `PlanningRuntime`. Preserve the plan controller and its active-item, terminal-history, phase, and finish gates; update the injected workflow constraint so providers understand that checkpoints are based on item transitions rather than tool calls.

**Tech Stack:** Python 3.11+, Loom runtime planning wrapper, async pytest, fake LLM provider integration tests, Ruff

## Global Constraints

- Forced plan mode requires planning but does not prescribe update frequency.
- The runtime must not infer plan completion from tool calls or outputs.
- Exactly one plan item must be `in_progress` before normal tools execute.
- `finish` must remain blocked until every plan item is completed or skipped.
- Historical `PLAN_UPDATE_REQUIRED` trace events must remain readable by the TUI.
- Non-planning ReAct behavior, tool-failure recovery, trace schemas, and TUI behavior must not change.

## File Structure

- Modify `src/loom/runtime/planning.py`: remove execution-batch state/enforcement and inject semantic checkpoint instructions.
- Modify `tests/runtime/test_planning.py`: replace transport-batch tests with active-item autonomy and transition tests.
- Modify `tests/tasks/test_task_runner.py`: add a runner-level one-tool-per-response regression sequence.
- Modify `docs/superpowers/specs/2026-08-01-tool-failure-plan-barrier-design.md`: mark the superseded batch barrier section and link to the new semantics so architecture documentation is not contradictory.

---

### Task 1: Remove Transport-Level Plan Update Enforcement

**Files:**
- Modify: `src/loom/runtime/planning.py:244-550`
- Modify: `tests/runtime/test_planning.py:295-395`

**Interfaces:**
- Consumes: existing `PlanController.update()`, `PlanningRuntime.wrap_tools()`, and normal tool handler options.
- Produces: unchanged public `PlanningRuntime` API without `llm_call_id`-based barrier state.

- [ ] **Step 1: Replace the batch tests with failing LLM-autonomy tests**

Replace `test_execution_batch_allows_same_batch_and_blocks_next_batch_until_plan_update`, `test_failed_execution_batch_also_requires_plan_update`, and `test_successful_plan_update_clears_execution_batch_barrier` with tests equivalent to:

```python
@pytest.mark.asyncio
async def test_active_item_allows_tools_across_multiple_llm_responses():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect", "Verify"))
    planning.controller.update(
        "start inspection",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Verify", "status": "pending", "note": None},
        ),
    )
    calls = []

    async def handler(_input, options):
        calls.append(options["metadata"]["llm_call_id"])
        return ok(Observation(f"obs-{len(calls)}", "read_file", {"ok": True}, _now()))

    read_file = planning.wrap_tools({"read_file": handler})["read_file"]
    first = await read_file({}, {"metadata": {"llm_call_id": "llm-1"}})
    second = await read_file({}, {"metadata": {"llm_call_id": "llm-2"}})
    third = await read_file({}, {"metadata": {"llm_call_id": "llm-3"}})

    assert all(result.ok and result.value.source == "read_file" for result in (first, second, third))
    assert calls == ["llm-1", "llm-2", "llm-3"]
```

Add a second test in which the first handler returns `VALIDATION_FAILED` and a different normal tool from a new `llm_call_id` still executes. Add a third test that submits one update marking `step_1=completed` and `step_2=in_progress`, then verifies subsequent tools execute under `step_2`.

- [ ] **Step 2: Run the new runtime tests and verify failure**

Run: `uv run pytest -q tests/runtime/test_planning.py -k 'across_multiple_llm or failure_does_not_require or semantic_item_transition'`

Expected: the cross-response and failure tests fail with a `planning.guard` observation whose code is `PLAN_UPDATE_REQUIRED`.

- [ ] **Step 3: Remove execution-batch state and guard branches**

Delete these private members and helper from `PlanningRuntime`:

```python
self._execution_batch_id
self._plan_update_required
PlanningRuntime._llm_batch_id()
```

Remove the `update_plan` success branch that clears them. In `_guard_handler`, remove batch-ID extraction, the `PLAN_UPDATE_REQUIRED` rejection, and the post-handler dirty-state mutation. Leave phase checks, exactly-one-active-item validation, finish validation, completion emission, and underlying handler behavior unchanged.

- [ ] **Step 4: Run all planning runtime tests**

Run: `uv run pytest -q tests/runtime/test_planning.py`

Expected: PASS.

- [ ] **Step 5: Commit runtime semantics**

```bash
git add src/loom/runtime/planning.py tests/runtime/test_planning.py
git commit -m "fix: let llm choose plan checkpoints"
```

---

### Task 2: Clarify Workflow Prompt and Verify the Full Task Loop

**Files:**
- Modify: `src/loom/runtime/planning.py:466-485`
- Modify: `tests/runtime/test_planning.py:380-400`
- Modify: `tests/tasks/test_task_runner.py`
- Modify: `docs/superpowers/specs/2026-08-01-tool-failure-plan-barrier-design.md`

**Interfaces:**
- Consumes: `make_task_loop()`, `run_generic_task()`, fake provider conventions in `tests/tasks/test_task_runner.py`.
- Produces: semantic checkpoint workflow text and a runner-level regression proving one-tool-per-response providers do not encounter `PLAN_UPDATE_REQUIRED`.

- [ ] **Step 1: Write a failing workflow-description test**

Replace the batch-oriented assertion with:

```python
def test_workflow_description_uses_semantic_plan_checkpoints():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect", "Verify"))

    description = planning._workflow_description(planning.controller.state)

    assert "using as many normal tools as needed" in description
    assert "only when item status" in description
    assert "mark the current item completed or skipped and the next item in_progress" in description
    assert "after each normal-tool batch" not in description
```

- [ ] **Step 2: Run the workflow test and verify failure**

Run: `uv run pytest -q tests/runtime/test_planning.py -k semantic_plan_checkpoints`

Expected: FAIL because the current prompt requires updates after each normal-tool batch.

- [ ] **Step 3: Replace the workflow instruction**

Use this execution prefix in `_workflow_description()`:

```text
Execute the active checklist item using as many normal tools as needed. Call update_plan with the complete checklist snapshot only when item status, a material note, or future work changes. When moving forward, mark the current item completed or skipped and the next item in_progress in the same update. Finish only after every item is terminal.
```

- [ ] **Step 4: Add the runner-level one-tool-per-response regression**

Using the existing fake provider and task harness helpers, provide this response sequence:

```text
submit_plan
update_plan(step_1=in_progress)
read_file from llm response A
read_file from llm response B
shell_execute from llm response C
update_plan(step_1=completed, step_2=in_progress)
shell_execute from llm response D
update_plan(all items completed)
finish
final answer
```

Assert the run succeeds, all four normal handlers execute, no observation has code `PLAN_UPDATE_REQUIRED`, plan revisions are monotonic, and the plan completes.

- [ ] **Step 5: Run the runner and integration-focused tests**

Run:

```bash
uv run pytest -q tests/tasks/test_task_runner.py tests/integration/test_end_to_end_loom_flow.py tests/integration/test_complex_llm_tool_calling.py
```

Expected: PASS.

- [ ] **Step 6: Reconcile the superseded barrier documentation**

At the top of `docs/superpowers/specs/2026-08-01-tool-failure-plan-barrier-design.md`, add a supersession note stating that its recoverable-tool-failure and sticky-tail sections remain valid, but its `llm_call_id` plan update barrier is replaced by `2026-08-01-llm-driven-plan-checkpoints-design.md` because an LLM response is not a semantic execution checkpoint.

- [ ] **Step 7: Run complete verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

Expected: all tests pass, Ruff reports no lint or formatting errors, and `git diff --check` emits no output.

- [ ] **Step 8: Commit integration coverage and documentation**

```bash
git add src/loom/runtime/planning.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py docs/superpowers/specs/2026-08-01-tool-failure-plan-barrier-design.md
git commit -m "test: cover llm-driven plan progression"
```

---

## Completion Criteria

- A plan with one active item permits normal tools from arbitrarily different `llm_call_id` values.
- Tool validation/execution failure does not impose a plan update.
- The LLM records item completion and handoff through one semantic `update_plan` snapshot.
- The runtime still prevents normal tools without one active item and prevents premature finish.
- New runs cannot emit `PLAN_UPDATE_REQUIRED` from normal-tool sequencing.
- The workflow prompt explicitly rejects per-tool update behavior.
- Focused tests, full suite, Ruff, formatting, and diff checks pass.
