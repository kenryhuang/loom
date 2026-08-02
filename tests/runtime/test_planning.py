from types import SimpleNamespace

import pytest

from loom.core import (
    AffordanceLayer,
    Context,
    GoalLayer,
    IdentityLayer,
    KnowledgeLayer,
    MinimalLoopDefinition,
    Observation,
    StateLayer,
    StepResult,
    ToolRef,
    Trace,
    err,
    make_loom_error,
    ok,
)
from loom.runtime.planning import PlanController, PlanItemStatus, PlanMode, PlanningRuntime, PlanPhase


def _now():
    return "2026-07-31T00:00:00Z"


def _ids():
    values = iter(("plan_test", "step_1", "step_2", "step_3", "step_4"))
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
        (
            {"id": "step_1", "content": "Different", "status": "pending", "note": None},
            {"id": "step_2", "content": "Implement carefully", "status": "in_progress", "note": None},
            {"id": "step_3", "content": "Verify", "status": "pending", "note": None},
        ),
    )

    assert first.ok and second.ok
    assert second.value.items[-1].id == "step_3"
    assert not illegal.ok
    assert illegal.error.code == "PLAN_TERMINAL_HISTORY_IMMUTABLE"


def test_update_rejects_two_active_items():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect", "Implement"))

    result = controller.update(
        "too much",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Implement", "status": "in_progress", "note": None},
        ),
    )

    assert not result.ok
    assert result.error.code == "PLAN_MULTIPLE_ACTIVE_ITEMS"


def test_update_requires_skip_reason():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect",))

    result = controller.update(
        "skip",
        ({"id": "step_1", "content": "Inspect", "status": "skipped", "note": ""},),
    )

    assert not result.ok
    assert result.error.code == "PLAN_SKIP_REASON_REQUIRED"


def test_finish_requires_all_items_terminal():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect",))

    premature = controller.complete()
    controller.update("done", ({"id": "step_1", "content": "Inspect", "status": "completed", "note": None},))
    completed = controller.complete()

    assert not premature.ok
    assert premature.error.code == "PLAN_INCOMPLETE"
    assert completed.ok
    assert controller.state.phase is PlanPhase.COMPLETED
    assert [event.event_type for event in controller.drain_events()][-1] == "plan.completed"


def test_wrong_phase_calls_are_rejected_and_controllers_are_isolated():
    left = _controller()
    right = _controller()

    wrong = left.submit("too early", ("Inspect",))
    entered = right.enter("complex")

    assert not wrong.ok
    assert wrong.error.code == "PLAN_PHASE_INVALID"
    assert left.state.phase is PlanPhase.INACTIVE
    assert entered.ok
    assert right.state.phase is PlanPhase.PLANNING


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


@pytest.mark.asyncio
async def test_plan_handlers_return_canonical_snapshots_and_rejections_are_recoverable():
    planning = PlanningRuntime(PlanMode.AUTO, id_factory=_ids(), now=_now)
    handlers = planning.wrap_tools({})

    entered = await handlers["enter_plan"]({"reason": "dependent work"}, {})
    submitted = await handlers["submit_plan"]({"explanation": "inspect first", "items": [{"content": "Inspect"}]}, {})
    duplicate_submit = await handlers["submit_plan"]({"explanation": "again", "items": [{"content": "Again"}]}, {})

    assert entered.ok and entered.value.value["accepted"] is True
    assert entered.value.value["plan"]["phase"] == "planning"
    assert entered.value.metadata["controlFlow"]["stepBoundary"] is True
    assert submitted.ok and submitted.value.value["plan"]["items"][0]["id"] == "step_1"
    assert submitted.value.metadata["controlFlow"]["stepBoundary"] is True
    assert duplicate_submit.ok
    assert duplicate_submit.value.source == "planning.guard"
    assert duplicate_submit.value.value["code"] == "PLAN_PHASE_INVALID"
    assert duplicate_submit.value.metadata is None


@pytest.mark.asyncio
async def test_update_plan_schema_and_handler_allow_omitted_note():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    update_ref = next(tool for tool in planning.tool_refs() if tool.id == "update_plan")
    item_schema = update_ref.input_schema["properties"]["items"]["items"]
    planning.controller.submit("plan", ("Inspect",))

    result = await planning.wrap_tools({})["update_plan"](
        {
            "explanation": "started",
            "items": [{"id": "step_1", "content": "Inspect", "status": "in_progress"}],
        },
        {},
    )

    assert item_schema["required"] == ("content", "status")
    assert result.ok and result.value.value["accepted"] is True
    assert result.value.value["plan"]["items"][0]["note"] is None
    assert result.value.metadata == {
        "controlFlow": {"stepBoundary": True, "reason": "planning_transition"}
    }


def test_update_plan_tool_advertises_full_snapshot_semantics():
    planning = PlanningRuntime(PlanMode.AUTO, id_factory=_ids(), now=_now)
    update_ref = next(tool for tool in planning.tool_refs() if tool.id == "update_plan")
    items_schema = update_ref.input_schema["properties"]["items"]

    assert "complete checklist snapshot" in update_ref.description.lower()
    assert items_schema["description"] == ("The complete replacement checklist. Include every item that must remain; omitted non-terminal items are removed.")


@pytest.mark.asyncio
async def test_finish_gate_requires_terminal_plan_then_preserves_handler_result():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect",))
    called = 0

    async def finish_handler(_input, _options=None):
        nonlocal called
        called += 1
        return ok(Observation("finish-obs", "finish", {"report": "done"}, _now()))

    handlers = planning.wrap_tools({"finish": finish_handler})
    premature = await handlers["finish"]({"report": "early"}, {})
    planning.controller.update(
        "done",
        ({"id": "step_1", "content": "Inspect", "status": "completed", "note": None},),
    )
    finished = await handlers["finish"]({"report": "done"}, {})

    assert premature.ok and premature.value.value["code"] == "PLAN_INCOMPLETE"
    assert premature.value.metadata is None
    assert called == 1
    assert finished.ok and finished.value.source == "finish"
    assert finished.value.metadata == {
        "controlFlow": {"stepBoundary": True, "reason": "planning_transition"}
    }
    assert planning.controller.state.phase is PlanPhase.COMPLETED


@pytest.mark.asyncio
async def test_finish_rejects_later_stale_tools_from_the_same_batch():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect",))
    planning.controller.update(
        "done",
        ({"id": "step_1", "content": "Inspect", "status": "completed"},),
    )
    calls = []

    async def handler(_input, options=None):
        calls.append(options["name"])
        return ok(Observation(f"obs-{len(calls)}", options["name"], {"ok": True}, _now()))

    handlers = planning.wrap_tools({"finish": handler, "read_file": handler})

    finished = await handlers["finish"]({}, {"name": "finish"})
    stale_read = await handlers["read_file"]({}, {"name": "read_file"})
    stale_finish = await handlers["finish"]({}, {"name": "finish-again"})

    assert finished.ok and finished.value.source == "finish"
    assert calls == ["finish"]
    assert stale_read.value.value["code"] == "PLAN_PHASE_INVALID"
    assert stale_finish.value.value["code"] == "PLAN_PHASE_INVALID"


@pytest.mark.asyncio
async def test_guard_does_not_convert_underlying_business_tool_errors():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect",))
    planning.controller.update(
        "started",
        ({"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},),
    )

    async def failing_handler(_input, _options=None):
        return err(make_loom_error("TOOL_FAILED", "boom", retryable=False))

    result = await planning.wrap_tools({"read_file": failing_handler})["read_file"]({}, {})

    assert not result.ok
    assert result.error.code == "TOOL_FAILED"


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


@pytest.mark.asyncio
async def test_tool_failure_does_not_require_plan_update_before_recovery():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect",))
    planning.controller.update(
        "started",
        ({"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},),
    )
    calls = []

    async def handler(_input, options):
        calls.append(options["name"])
        if options["name"] == "edit_file":
            return err(make_loom_error("VALIDATION_FAILED", "old_text missing", retryable=False))
        return ok(Observation("obs-read", "read_file", {"ok": True}, _now()))

    handlers = planning.wrap_tools({"edit_file": handler, "read_file": handler})

    failed = await handlers["edit_file"]({}, {"name": "edit_file", "metadata": {"llm_call_id": "llm-1"}})
    recovered = await handlers["read_file"]({}, {"name": "read_file", "metadata": {"llm_call_id": "llm-2"}})

    assert not failed.ok and failed.error.code == "VALIDATION_FAILED"
    assert recovered.ok and recovered.value.source == "read_file"
    assert calls == ["edit_file", "read_file"]


@pytest.mark.asyncio
async def test_semantic_item_transition_allows_tools_under_next_active_item():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect", "Verify"))
    planning.controller.update(
        "started",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Verify", "status": "pending", "note": None},
        ),
    )
    calls = []

    async def handler(_input, options):
        calls.append(options["metadata"]["llm_call_id"])
        return ok(Observation(f"obs-{len(calls)}", "read_file", {"ok": True}, _now()))

    handlers = planning.wrap_tools({"read_file": handler})
    await handlers["read_file"]({}, {"metadata": {"llm_call_id": "llm-1"}})
    updated = await handlers["update_plan"](
        {
            "explanation": "inspection complete; begin verification",
            "items": [
                {"id": "step_1", "content": "Inspect", "status": "completed", "note": "Reviewed core files"},
                {"id": "step_2", "content": "Verify", "status": "in_progress", "note": None},
            ],
        },
        {"metadata": {"llm_call_id": "llm-2"}},
    )
    next_item_tool = await handlers["read_file"]({}, {"metadata": {"llm_call_id": "llm-3"}})

    assert updated.value.value["accepted"] is True
    assert planning.controller.state.items[0].status is PlanItemStatus.COMPLETED
    assert planning.controller.state.items[1].status is PlanItemStatus.IN_PROGRESS
    assert next_item_tool.value.source == "read_file"
    assert calls == ["llm-1", "llm-3"]


def test_workflow_description_uses_semantic_plan_checkpoints():
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)
    planning.controller.submit("plan", ("Inspect", "Verify"))

    description = planning._workflow_description(planning.controller.state)

    assert "using as many normal tools as needed" in description
    assert "only when item status" in description
    assert "mark the current item completed or skipped and the next item in_progress" in description
    assert "after each normal-tool batch" not in description


class RecordingSink:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return ok(None)


def _context(*tools):
    return Context(
        id="context_test",
        run_id="run_test",
        created_at=_now(),
        identity=IdentityLayer(role="test loop"),
        goal=GoalLayer(objective="test planning"),
        state=StateLayer(),
        knowledge=KnowledgeLayer(),
        affordances=AffordanceLayer(tools=tools),
    )


def _step_result_for(context):
    trace = Trace(
        id="trace_result",
        run_id=context.run_id,
        loop_id="loop_test",
        loop_version="v1",
        step_number=0,
        root_trace_id="trace_result",
        started_at=_now(),
        ended_at=_now(),
        duration_ms=0,
        input_context_id=context.id,
        output_context_id=context.id,
        outcome="pass",
    )
    return StepResult(context, trace)


def _runtime(sink):
    return SimpleNamespace(
        run_id="run_test",
        loop_id="loop_test",
        trace_id="trace_test",
        trace_sink=sink,
        now=_now,
    )


@pytest.mark.asyncio
async def test_planning_wrapper_persists_snapshot_emits_events_and_blocks_done():
    seen_tools = []
    emitted = RecordingSink()
    planning = PlanningRuntime(PlanMode.FORCE, id_factory=_ids(), now=_now)

    async def base_step(context, _runtime_value):
        seen_tools.append(tuple(tool.id for tool in context.affordances.tools))
        await planning.wrap_tools({})["submit_plan"]({"explanation": "plan", "items": [{"content": "Inspect"}]}, {})
        return ok(_step_result_for(context))

    definition = MinimalLoopDefinition(
        id="loop_test",
        version="v1",
        identity=IdentityLayer(role="test loop"),
        goal=GoalLayer(objective="test planning"),
        step=base_step,
        done=lambda _context_value, _runtime_value: ok(True),
    )
    wrapped = planning.wrap_loop(definition)

    stepped = await wrapped.step(_context(ToolRef("read_file", "read")), _runtime(emitted))
    done = await wrapped.done(stepped.value.context, SimpleNamespace())

    assert seen_tools == [("submit_plan",)]
    assert stepped.value.context.state.scratch["plan"]["phase"] == "executing"
    assert done.value is False
    assert [event["type"] for event in emitted.events] == ["plan.entered", "plan.submitted"]


@pytest.mark.asyncio
async def test_wrapper_reprojects_tools_and_replaces_workflow_constraint_each_step():
    planning = PlanningRuntime(PlanMode.AUTO, id_factory=_ids(), now=_now)
    sink = RecordingSink()
    seen = []

    async def base_step(context, _runtime_value):
        seen.append(
            (
                tuple(tool.id for tool in context.affordances.tools),
                tuple(constraint.id for constraint in context.identity.constraints),
            )
        )
        handlers = planning.wrap_tools({})
        if planning.controller.state.phase is PlanPhase.INACTIVE:
            await handlers["enter_plan"]({"reason": "complex"}, {})
        else:
            await handlers["submit_plan"]({"explanation": "plan", "items": [{"content": "Inspect"}]}, {})
        return ok(_step_result_for(context))

    definition = MinimalLoopDefinition(
        id="loop_test",
        version="v1",
        identity=IdentityLayer(role="test loop"),
        goal=GoalLayer(objective="test planning"),
        step=base_step,
        done=lambda _context_value, _runtime_value: ok(False),
    )
    wrapped = planning.wrap_loop(definition)
    first = await wrapped.step(_context(ToolRef("read_file", "read")), _runtime(sink))
    await wrapped.step(first.value.context, _runtime(sink))

    assert seen == [
        (("read_file", "enter_plan"), ("runtime-plan-workflow",)),
        (("submit_plan",), ("runtime-plan-workflow",)),
    ]


def test_off_mode_returns_exact_legacy_definition_and_handlers():
    definition = MinimalLoopDefinition(
        id="loop_test",
        version="v1",
        identity=IdentityLayer(role="test loop"),
        goal=GoalLayer(objective="test planning"),
        step=lambda context, _runtime_value: ok(_step_result_for(context)),
        done=lambda _context_value, _runtime_value: ok(True),
    )
    handlers = {"read_file": lambda _input, _options=None: ok(None)}
    planning = PlanningRuntime(PlanMode.OFF)

    assert planning.wrap_loop(definition) is definition
    assert planning.wrap_tools(handlers) is handlers
