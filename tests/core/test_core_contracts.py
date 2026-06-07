from dataclasses import FrozenInstanceError

import pytest

from loom.core import (
    Action,
    AffordanceLayer,
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    KnowledgeItem,
    MinimalLoopDefinition,
    Observation,
    Result,
    StateLayer,
    Trace,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    empty_state,
    err,
    freeze_context,
    is_err,
    is_ok,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    ok,
    to_loom_error,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_ids_use_stable_prefixes_and_validate_step_numbers():
    assert new_loop_id().startswith("loop_")
    assert new_context_id().startswith("ctx_")
    assert new_trace_id().startswith("trace_")
    assert new_run_id().startswith("run_")
    assert new_loop_version() == "v1"
    assert as_step_number(0) == 0
    assert as_step_number(3) == 3

    with pytest.raises(ValueError, match="StepNumber"):
        as_step_number(-1)
    with pytest.raises(ValueError, match="StepNumber"):
        as_step_number(1.5)


def test_result_and_loom_error_helpers():
    success = ok(42)
    failure_error = make_loom_error("VALIDATION_FAILED", "bad input", retryable=False)
    failure = err(failure_error)

    assert success == Result.ok(42)
    assert is_ok(success)
    assert not is_err(success)
    assert is_err(failure)
    assert not is_ok(failure)
    assert failure.error == failure_error

    mapped = to_loom_error(ValueError("boom"))
    assert mapped.code == "INTERNAL"
    assert mapped.message == "boom"
    assert mapped.retryable is False
    assert mapped.cause == {"name": "ValueError", "message": "boom"}
    assert to_loom_error(failure_error) is failure_error


def test_context_empty_layers_and_freeze_contract():
    assert empty_state() == StateLayer(observations=(), decisions=(), pending=())
    assert empty_knowledge().version == "v1"
    assert empty_knowledge().facts == ()
    assert empty_affordances() == AffordanceLayer(tools=(), loops=(), resources=())

    context = make_context()
    frozen = freeze_context(context)

    assert frozen.identity.role == "counter"
    assert frozen.goal.objective == "Count once"
    assert frozen.state.observations[0].value == {"count": 0}
    assert frozen.knowledge.facts[0].kind == "fact"
    assert frozen.affordances.tools == ()

    with pytest.raises(FrozenInstanceError):
        frozen.identity.role = "mutated"
    with pytest.raises(AttributeError):
        frozen.state.observations.append(Observation("obs-2", "test", {"count": 1}, NOW))


def test_trace_and_minimal_loop_contracts():
    context = make_context()
    loop_id = new_loop_id()
    version = new_loop_version()
    trace_id = new_trace_id()
    trace = Trace(
        id=trace_id,
        run_id=context.run_id,
        loop_id=loop_id,
        loop_version=version,
        step_number=as_step_number(0),
        root_trace_id=trace_id,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=0,
        input_context_id=context.id,
        output_context_id=context.id,
        outcome="pass",
    )

    async def step_fn(input_context, runtime):
        return ok({"context": input_context, "trace": trace})

    def done_fn(_input_context, _runtime):
        return ok(True)

    definition = MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="contract loop"),
        goal=GoalLayer(objective="Define loop"),
        step=step_fn,
        done=done_fn,
    )

    assert trace.outcome == "pass"
    assert trace.observations == ()
    assert definition.identity.role == "contract loop"
    assert definition.version == "v1"


def make_context():
    fact = KnowledgeItem(
        id="fact-1",
        kind="fact",
        content="known",
        confidence=0.9,
        created_at=NOW,
    )
    observation = Observation("obs-1", "test", {"count": 0}, NOW)
    action = Action("action-1", "custom", "Record")
    decision = Decision("decision-1", action, "Because", alternatives=(), confidence=1, at=NOW)
    return Context(
        id=new_context_id(),
        run_id=new_run_id(),
        created_at=NOW,
        identity=IdentityLayer(role="counter"),
        goal=GoalLayer(objective="Count once"),
        state=StateLayer(observations=(observation,), decisions=(decision,), pending=()),
        knowledge=empty_knowledge(facts=(fact,)),
        affordances=empty_affordances(),
    )
