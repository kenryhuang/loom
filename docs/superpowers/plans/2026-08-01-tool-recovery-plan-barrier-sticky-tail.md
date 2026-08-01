# Tool Recovery, Plan Barrier, and Sticky-Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep recoverable tool failures inside the ReAct loop, require a checklist update between execution batches, and stop the TUI from stealing the user's scroll position.

**Architecture:** Runtime tool invocation classifies handler-domain errors without changing the `Result` abstraction. The LLM adapter converts only classified errors into protocol-valid observations, while `PlanningRuntime` tracks LLM batch identity and enforces an update barrier. `EventFeedWidget` owns sticky-tail state so every event producer follows one scrolling policy.

**Tech Stack:** Python 3.11, asyncio, immutable Loom core models, pytest/pytest-asyncio, Textual 8.x.

## Global Constraints

- Preserve the existing ReAct loop and both native and JSON-action tool paths.
- `ABORTED`, trace persistence, cancellation, and unclassified infrastructure errors remain terminal.
- Multiple normal tools emitted by one assistant response remain executable.
- `update_plan` remains a complete checklist snapshot; the runtime never invents item transitions.
- Do not change scrolling inside an individual event detail pane.
- Use strict red-green-refactor TDD and commit each independently testable task.

---

### Task 1: Classify Runtime Tool-Domain Failures

**Files:**
- Modify: `src/loom/runtime/engine.py:430-490`
- Test: `tests/runtime/test_runtime.py`

**Interfaces:**
- Consumes: `LoomError`, `Result`, `RegistryView`, and the existing `tool.failed` event contract.
- Produces: errors returned from resolved/missing tool handlers with `error.metadata["failureDomain"] == "tool"`; `ABORTED` and event-recorder errors remain unclassified.

- [ ] **Step 1: Write failing runtime classification tests**

Add tests that run real one-step loops through `runtime.call_tool()` and capture the returned error:

```python
def test_runtime_marks_handler_failure_as_tool_domain():
    async def scenario():
        async def broken(_input, _options):
            return err(make_loom_error("VALIDATION_FAILED", "old_text missing", retryable=False, metadata={"edit_index": 4}))

        captured = []
        async def step_fn(context, runtime):
            result = await runtime.call_tool("edit_file", {"path": "sample.py"})
            captured.append(result)
            return result

        definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="runtime failure test"),
            goal=GoalLayer(objective="classify a failed tool"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
        store = InMemoryTraceStore()
        handle = create(
            definition,
            trace_store=store,
            registry=create_runtime_registry(tools={"edit_file": broken}),
        ).unwrap()
        await run(handle, make_context(), max_steps=1)
        assert captured[0].error.metadata == {"edit_index": 4, "failureDomain": "tool"}
        assert [event["type"] for event in store.events()].count("tool.failed") == 1

def test_runtime_does_not_mark_aborted_handler_failure_as_tool_domain():
    # Same real-runtime setup, with handler returning LoomError(code="ABORTED").
    assert "failureDomain" not in (captured[0].error.metadata or {})

def test_runtime_marks_missing_handler_as_tool_domain():
    # Use an empty real RuntimeRegistry and call a non-advertised tool ID.
    assert captured[0].error.code == "VALIDATION_FAILED"
    assert captured[0].error.metadata["failureDomain"] == "tool"

def test_runtime_does_not_reclassify_trace_sink_failure():
    # Recorder rejects tool.started before handler invocation.
    assert captured[0].error.code == "TRACE_WRITE_FAILED"
    assert "failureDomain" not in (captured[0].error.metadata or {})
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/runtime/test_runtime.py -k 'tool_domain or reclassify_trace' -vv`

Expected: handler-domain metadata assertions fail because `call_tool()` currently returns the original error unchanged.

- [ ] **Step 3: Add a narrow immutable error classifier**

In `engine.py`, import `replace` and add:

```python
def _tool_domain_error(error: LoomError) -> LoomError:
    if error.code == "ABORTED":
        return error
    metadata = dict(error.metadata or {})
    metadata["failureDomain"] = "tool"
    return replace(error, metadata=metadata)
```

Apply it only after a missing handler is resolved as an error or after handler invocation returns/raises an error. Emit `tool.failed` with the classified error and return that same classified `Result`. Return failures from `emit(...)` unchanged.

- [ ] **Step 4: Run focused and runtime tests GREEN**

Run: `uv run pytest tests/runtime/test_runtime.py -vv`

Expected: all runtime tests pass and each failed tool emits one canonical `tool.failed` event.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/loom/runtime/engine.py tests/runtime/test_runtime.py
git commit -m "fix: classify recoverable tool failures"
```

---

### Task 2: Feed Recoverable Failures Back to the LLM

**Files:**
- Modify: `src/loom/llm/api.py:393-466,1256-1334`
- Test: `tests/llm/test_llm.py`

**Interfaces:**
- Consumes: Task 1's `failureDomain="tool"` metadata and the current `llm_call_id`.
- Produces: `_recoverable_tool_failure(error: LoomError | None) -> bool` and `_tool_failure_observation(error: LoomError, *, observation_id: str, source: str, at: str) -> Result`; native failures become `role="tool"` messages and JSON-action failures become transcript feedback.

- [ ] **Step 1: Write failing native-path recovery tests**

Add a fake-provider sequence: invalid native `edit_file`, corrected native `edit_file`, final decision. The real adapter-facing fake runtime returns a classified error on the first call and an observation on the second.

```python
assert result.ok
assert len(tool_calls) == 2
failure_message = next(message for message in provider.messages[1][0] if message.role == "tool")
assert json.loads(failure_message.content) == {
    "ok": False,
    "error": {
        "code": "VALIDATION_FAILED",
        "message": "old_text was not found",
        "retryable": False,
        "metadata": {"edit_index": 4, "failureDomain": "tool"},
    },
}
assert result.value.trace.observations[0].value["ok"] is False
assert tool_calls[0][2]["metadata"]["llm_call_id"].endswith("-llm-1")
assert tool_calls[1][2]["metadata"]["llm_call_id"].endswith("-llm-2")
```

Also assert that a failed required tool keeps `tool_choice="required"` on the next request and that the failed attempt consumes the tool-call budget.

- [ ] **Step 2: Run native recovery tests and verify RED**

Run: `uv run pytest tests/llm/test_llm.py -k 'recoverable_tool_failure or failed_required_tool or failed_tool_consumes' -vv`

Expected: the first classified error terminates the step and no second provider request occurs.

- [ ] **Step 3: Implement native failure observations and batch metadata**

Add helpers equivalent to:

```python
def _recoverable_tool_failure(error: LoomError | None) -> bool:
    return bool(error and error.code != "ABORTED" and (error.metadata or {}).get("failureDomain") == "tool")

def _tool_failure_observation(error: LoomError, *, observation_id: str, source: str, at: str) -> Result:
    try:
        return ok(Observation(
            observation_id,
            source,
            freeze_json({
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "metadata": dict(error.metadata or {}),
                },
            }),
            at,
        ))
    except BaseException as exc:
        return err(make_loom_error(
            "INTERNAL",
            "Failed to construct tool failure observation",
            retryable=False,
            cause={"name": type(exc).__name__, "message": str(exc)},
        ))
```

For every attempted native call, add `llm_call_id` to metadata and increment the tool budget before branching on the result. Convert classified errors to observations, returning `INTERNAL` if construction fails; append the required `role="tool"` response and transcript entry, and continue. Discard a required-tool obligation only after success. Return unclassified and `ABORTED` errors unchanged.

- [ ] **Step 4: Write and verify RED for JSON-action parity**

Use provider output whose parsed action targets a real tool; make its first result a classified error and its second result successful. Assert the next provider message contains `"ok":false`, the step completes, and JSON-action metadata contains both `tool_call_source="json_action"` and `llm_call_id`.

Run: `uv run pytest tests/llm/test_llm.py -k 'json_tool_failure_feedback' -vv`

Expected: failure is still terminal before the implementation is extended to this path.

- [ ] **Step 5: Implement JSON-action parity and retain terminal errors**

Use the same conversion helper and counter ordering in the JSON-action loop. Add explicit tests showing `ABORTED` and an unclassified `INTERNAL` error still return `Result.err` immediately.

- [ ] **Step 6: Run the complete LLM test module GREEN**

Run: `uv run pytest tests/llm/test_llm.py -vv`

Expected: all LLM adapter tests pass with protocol-valid native message ordering.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "fix: recover from tool failures in llm loop"
```

---

### Task 3: Enforce the Plan Update Barrier per LLM Batch

**Files:**
- Modify: `src/loom/runtime/planning.py:245-550`
- Test: `tests/runtime/test_planning.py`

**Interfaces:**
- Consumes: `options["metadata"]["llm_call_id"]` supplied by Task 2.
- Produces: run-local `_execution_batch_id` and `_plan_update_required`; successful `update_plan` clears them; rejected new batches return a `planning.guard` observation with `code="PLAN_UPDATE_REQUIRED"`.

- [ ] **Step 1: Write failing same-batch and next-batch tests**

Exercise wrapped real handlers with an executing plan and one active item:

```python
first = await handlers["read_file"]({}, {"metadata": {"llm_call_id": "llm-1"}})
same_batch = await handlers["shell_execute"]({}, {"metadata": {"llm_call_id": "llm-1"}})
blocked = await handlers["write_file"]({}, {"metadata": {"llm_call_id": "llm-2"}})

assert first.value.source == "read_file"
assert same_batch.value.source == "shell_execute"
assert blocked.value.source == "planning.guard"
assert blocked.value.value["code"] == "PLAN_UPDATE_REQUIRED"
assert calls == ["read_file", "shell_execute"]
```

Add a second test where the first handler returns `Result.err`; assert a different next batch is blocked while the underlying failed result remains unchanged.

- [ ] **Step 2: Run barrier tests and verify RED**

Run: `uv run pytest tests/runtime/test_planning.py -k 'execution_batch or plan_update_barrier' -vv`

Expected: the new batch executes because current planning guards only validate active items.

- [ ] **Step 3: Implement batch state and dirtying**

Initialize state in `PlanningRuntime.__init__`:

```python
self._execution_batch_id: str | None = None
self._plan_update_required = False
```

Resolve batch IDs only from `options.metadata.llm_call_id`. Before invoking a normal handler in `EXECUTING`, reject a different non-empty batch while dirty. After any underlying normal handler result—success or failure—record its non-empty batch ID and set the dirty flag. Calls without LLM batch metadata preserve compatibility and do not participate in this barrier.

- [ ] **Step 4: Write failing clear-on-update and prompt tests**

After dirtying batch `llm-1`, call the real `update_plan` handler with a valid full snapshot, then execute batch `llm-2`. Assert revision increment, handler execution, and that the workflow constraint explicitly requires an update after every normal-tool batch.

- [ ] **Step 5: Clear only after accepted updates**

In the `update_plan` handler, call `_transition_result` first; clear batch state only if the returned observation has `accepted=True`. An invalid/rejected update leaves the barrier closed. Update `_workflow_description()` to tell the model to update after each batch and before the next normal tool batch.

- [ ] **Step 6: Run planning and forced-task tests GREEN**

Run: `uv run pytest tests/runtime/test_planning.py tests/tasks/test_task_runner.py -vv`

Expected: existing forced plans still work because `update_plan` and the next normal call may share an assistant response.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/loom/runtime/planning.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py
git commit -m "feat: require plan updates between tool batches"
```

---

### Task 4: Add End-to-End Task Recovery Coverage

**Files:**
- Modify: `tests/tasks/test_task_runner.py`

**Interfaces:**
- Consumes: Tasks 1-3 through `run_generic_task(request, provider=provider, options=TaskRunOptions(plan_mode=PlanMode.FORCE))`.
- Produces: a regression scenario matching the yakDB failure: submit, start, invalid edit, update, corrected edit, finish.

- [ ] **Step 1: Write the full fake-provider scenario**

Create `RecoveringForcedPlanProvider` whose responses issue these batches:

```text
submit_plan
update_plan(in_progress) + edit_file(invalid exact match)
update_plan(note failure/remain in_progress) + edit_file(corrected exact match)
update_plan(completed) + finish
final decision
```

The provider captures each request transcript. Assert:

```python
assert result.ok
assert target.read_text(encoding="utf-8") == "corrected\n"
assert any(json.loads(message.content).get("ok") is False for message in provider.messages_seen[2] if message.role == "tool")
assert [event["type"] for event in sink.events].count("tool.failed") == 1
assert [event["type"] for event in sink.events].count("tool.completed") >= 2
assert [event["type"] for event in sink.events if event["type"] == "plan.updated"]
```

- [ ] **Step 2: Run the regression test and verify RED if any wiring is missing**

Run: `uv run pytest tests/tasks/test_task_runner.py -k 'recovers_from_failed_edit' -vv`

Expected before all wiring is complete: failure pinpoints missing propagation or barrier clearing. After Tasks 1-3 it may pass immediately as an integration confirmation; the lower-level tests already supplied the mandatory RED evidence for each production change.

- [ ] **Step 3: Fix only integration wiring defects, then run GREEN**

Do not add new recovery mechanisms here. Correct only metadata propagation, fake-provider sequencing, or public runner wiring exposed by the end-to-end test.

Run: `uv run pytest tests/tasks/test_task_runner.py -vv`

- [ ] **Step 4: Commit Task 4**

```bash
git add tests/tasks/test_task_runner.py src/loom/llm/api.py src/loom/runtime/engine.py src/loom/runtime/planning.py
git commit -m "test: cover planned task tool recovery"
```

---

### Task 5: Centralize TUI Sticky-Tail Behavior

**Files:**
- Modify: `src/loom/tui/tui_app.py:1223-1300,1485-1540,1590-1628`
- Test: `tests/unit/test_tui_app.py`

**Interfaces:**
- Produces: `EventFeedWidget.follow_tail: bool`, `pause_following()`, `resume_following()`, and mutation methods that apply the policy.
- Consumes: Textual's `watch_scroll_y`, `is_vertical_scroll_end`, mouse scroll messages, and existing `j/k/g/G` actions.

- [ ] **Step 1: Replace the old plan-jump test with failing viewport-preservation tests**

Build enough events to overflow a `100x24` test app, move the feed upward, deliver `plan.updated`, a new event, and an LLM stream aggregate. Assert the same `scroll_y` remains and the selected event is unchanged.

```python
feed.scroll_to(y=max(0, feed.max_scroll_y - 5), animate=False, immediate=True)
await pilot.pause()
old_y = feed.scroll_y
old_selected = feed.get_selected_index()
app._handle_event(_plan_event("plan.updated", 2))
await pilot.pause()
assert feed.scroll_y == old_y
assert feed.get_selected_index() == old_selected
```

Add tests proving a new event at the bottom follows the new maximum and `plan.updated` does not select or scroll to the plan item.

- [ ] **Step 2: Run TUI preservation tests and verify RED**

Run: `uv run pytest tests/unit/test_tui_app.py -k 'follow_tail or preserves_scroll or plan_update' -vv`

Expected: the existing unconditional `scroll_end()` or `scroll_to_widget()` changes the viewport.

- [ ] **Step 3: Implement feed-owned sticky-tail state**

Add behavior equivalent to:

```python
def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    self._follow_tail = True

@property
def follow_tail(self) -> bool:
    return self._follow_tail

def pause_following(self) -> None:
    self._follow_tail = False

def resume_following(self) -> None:
    self._follow_tail = True
    self.scroll_end(animate=False)

def watch_scroll_y(self, old_value: float, new_value: float) -> None:
    super().watch_scroll_y(old_value, new_value)
    self._follow_tail = new_value >= self.max_scroll_y
```

Override upward mouse/page-scroll handlers to pause before delegating. In `add_event()`, only replace selection/collapse prior details and call `scroll_end()` when following was active; otherwise append without disturbing the reviewed item. In `update_event()`, request a tail scroll only while following.

- [ ] **Step 4: Remove event-handler scroll bypasses**

Delete the `plan.updated` `select_event()/scroll_to_widget()` block and the LLM aggregate's direct `scroll_end()`. Tool aggregation may select the latest execution only when `feed.follow_tail` is true.

- [ ] **Step 5: Add failing keyboard state-transition tests**

Use `pilot.press("k")`, `pilot.press("g")`, `pilot.press("G")`, and `pilot.press("j")` to assert upward intent pauses even if the viewport did not move, `G` resumes, and `j` resumes only on reaching the last event/bottom.

- [ ] **Step 6: Wire actions to the sticky-tail API and run GREEN**

`action_cursor_up` and `action_scroll_top` call `pause_following()` before navigation. `action_scroll_bottom` calls `resume_following()`. `action_cursor_down` resumes only when selection reaches the last event.

Run: `uv run pytest tests/unit/test_tui_app.py -vv`

- [ ] **Step 7: Commit Task 5**

```bash
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "fix: preserve manual tui scroll position"
```

---

### Task 6: Full Verification and Documentation Consistency

**Files:**
- Verify: `docs/superpowers/specs/2026-08-01-tool-failure-plan-barrier-design.md`
- Verify: all modified source and test files

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a clean, linted, fully tested main branch.

- [ ] **Step 1: Run formatting and static checks**

Run: `uv run ruff check src tests`

Expected: exit 0 with no diagnostics.

- [ ] **Step 2: Run focused regression suites together**

Run: `uv run pytest tests/runtime/test_runtime.py tests/llm/test_llm.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py tests/unit/test_tui_app.py -vv`

Expected: all selected tests pass; TUI tests may skip only if Textual is unavailable.

- [ ] **Step 3: Run the complete suite**

Run: `uv run pytest -q`

Expected: zero failures, with only the repository's documented environment-dependent skips.

- [ ] **Step 4: Audit the acceptance criteria and repository state**

Run:

```bash
git diff --check
git status --short
git log --oneline -8
```

Confirm each spec acceptance criterion maps to a passing test and no unrelated file is modified.

- [ ] **Step 5: Commit any verification-only cleanup**

Only if lint or the acceptance audit required a real change, stage the known files from this plan:

```bash
git add src/loom/runtime/engine.py src/loom/llm/api.py src/loom/runtime/planning.py src/loom/tui/tui_app.py tests/runtime/test_runtime.py tests/llm/test_llm.py tests/runtime/test_planning.py tests/tasks/test_task_runner.py tests/unit/test_tui_app.py
git commit -m "chore: finalize planned loop recovery"
```
