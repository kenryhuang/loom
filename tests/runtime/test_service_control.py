import asyncio

from loom.core import MinimalLoopDefinition, StepResult, Trace, new_trace_id, now_iso, ok
from loom.runtime import create, step
from loom.runtime.control import StepControl
from loom.runtime.planning import PlanningRuntime
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context


def test_suspension_is_a_normal_result_with_preserved_trace_identity():
    async def scenario():
        context = make_task_context(TaskRequest("Work")).unwrap()

        def step_fn(context, runtime):
            tid = new_trace_id()
            trace = Trace(tid, context.run_id, runtime.loop_id, "v1", 0, tid, now_iso(), now_iso(), 0, context.id, context.id, "paused")
            return ok(StepResult(context, trace, control=StepControl("paused", "user requested")))

        definition = MinimalLoopDefinition("loop_service", "v1", context.identity, context.goal, step_fn, lambda *_: False)
        handle = create(definition).unwrap()
        result = await step(handle, context, trace_id="trace_resume")
        assert result.ok
        assert result.value.control.kind == "paused"
        assert [e["type"] for e in handle.trace_reader.store.events()] == ["step.started", "step.suspended"]
        assert handle.trace_reader.store.events()[0]["trace_id"] == "trace_resume"

    asyncio.run(scenario())


def test_planning_participant_round_trip_and_invalid_version():
    planning = PlanningRuntime("auto")
    planning.route.select_react("direct")
    planning.route.mark_task_execution_started()
    snapshot = planning.snapshot()
    restored = PlanningRuntime("auto")
    assert restored.restore(snapshot).ok
    assert restored.snapshot() == snapshot
    assert not restored.restore({**snapshot, "schema_version": 99}).ok
