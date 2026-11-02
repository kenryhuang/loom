import pytest

from loom.autonomy.stage_planner import StagePlanner
from loom.autonomy.supervisor import AgentSupervisor
from tests.autonomy.test_stage_contracts import configure, delivery
from tests.autonomy.test_stage_end_to_end import decision


def setup_plan(facade, aid, **policy):
    configure(facade, aid, **policy)
    delivery(facade, aid)
    facade.supervisor.stages.planner.provider = decision
    facade.supervisor.stages.advance(aid)
    return facade.store.snapshot(aid)


def test_completed_stage_advances_despite_learning_gaps_and_daily_limit_waits(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid, max_new_stages_per_day=1)
    stage = snap["stages"][0]
    goal = next(g for g in snap["goals"] if g["id"] in stage["goal_ids"])
    delivery(facade, aid, goal)
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_learning_cycles", dict(id="partial", agent_id=aid, goal_id=goal["id"], state="deferred", coverage="partial"))
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    assert snap["stages"][0]["state"] == "completed"
    assert snap["stage_reviews"][0]["learning"][0]["coverage"] == "partial"
    facade.supervisor.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["stages"]) == 1
    assert "Daily" in facade.store.snapshot(aid)["objective_planning"][0]["reason"]
    facade.store.clock.advance(86400)
    facade.supervisor.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["stages"]) == 2


def test_stage_failure_cannot_be_completed_and_changed_method_has_bounded_backoff(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    delivery(facade, aid, goal, passed=False)
    facade.supervisor.stages.advance(aid)  # Fixture falsely claims success; host must reject it.
    assert facade.store.snapshot(aid)["objective_planning"][0]["state"] == "blocked"
    assert facade.store.snapshot(aid)["stages"][0]["state"] != "completed"


def test_failed_goal_repair_preserves_failure_and_advances_only_after_acceptance(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    delivery(facade, aid, goal, passed=False)

    def repair(agent, payload):
        return dict(
            action="review_stage",
            reason="Retry using direct file inspection",
            source_refs=[],
            result="repair",
            change="Read implementation before inferring structure",
            remaining_gap="Missing verified rendering structure",
            checks=[],
        )

    facade.supervisor.stages.planner.provider = repair
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    new = next(g for g in snap["goals"] if g.get("predecessor_id") == goal["id"])
    assert new["state"] == "queued" and new["not_before"] > facade.store.now()
    assert next(g for g in snap["goals"] if g["id"] == goal["id"])["state"] == "blocked"
    assert snap["stages"][0]["goal_ids"] == [new["id"]]
    facade.store.clock.advance(61)
    delivery(facade, aid, new)
    facade.supervisor.stages.planner.provider = decision
    facade.supervisor.stages.advance(aid)
    assert facade.store.snapshot(aid)["stages"][0]["state"] == "completed"
    facade.supervisor.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["stages"]) == 2  # Old failed Goal must not block a successfully repaired stage.


def test_durable_model_output_reused_after_restart_without_another_call(agent_service):
    service, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    calls = []

    class Crash(StagePlanner):
        def propose(self, agent, payload, checkpoint):
            calls.append(payload)
            checkpoint(decision(agent, payload))
            raise SystemExit("crash after model output")

    facade.supervisor.stages.planner = Crash(service, facade.store)
    with pytest.raises(SystemExit):
        facade.supervisor.stages.advance(aid)
    restored = AgentSupervisor(service, facade.store, service.background)
    restored.stages.planner.provider = lambda *args: pytest.fail("Must reuse recorded response")
    restored.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["stages"]) == 1
    assert len(calls) == 1
    restored.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["stages"]) == 1


def test_role_change_during_planning_supersedes_response(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)

    def concurrent(agent, payload):
        facade.store.command(aid, "pause-in-flight", "pause", agent["revision"])
        return decision(agent, payload)

    facade.supervisor.stages.planner.provider = concurrent
    facade.supervisor.stages.advance(aid)
    assert not facade.store.snapshot(aid)["stages"]
    with facade.store.sessions.transaction() as db:
        assert facade.store.rows(db, "agent_runs", aid)[0]["state"] == "superseded"


def test_restart_without_recorded_response_keeps_unknown_cost_and_does_not_resend(agent_service):
    from loom.autonomy.budget import BudgetLedger

    service, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    ledger = BudgetLedger(facade.store.sessions.path, clock=facade.store.clock)

    class LostResponse(StagePlanner):
        def propose(self, agent, payload, checkpoint):
            ledger.reserve("lost-call", dict(agent_id=aid, objective_id="quality"), 40, "stage_planning")
            ledger.settle("lost-call", None)
            raise SystemExit("response lost")

    facade.supervisor.stages.planner = LostResponse(service, facade.store)
    with pytest.raises(SystemExit):
        facade.supervisor.stages.advance(aid)
    restored = AgentSupervisor(service, facade.store, service.background)
    restored.stages.planner.provider = lambda *args: pytest.fail("Unknown request must not be resent automatically")
    restored.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    assert snap["objective_planning"][0]["state"] == "blocked"
    assert not snap["stages"]
    assert ledger.objective_summary(aid)["objectives"][0]["reserved_or_unknown"] == 40


def test_learning_backpressure_is_visible_and_does_not_call_stage_model(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    with facade.store.sessions.transaction() as db:
        for index in range(20):
            facade.store.put(db, "agent_learning_cycles", dict(id=f"pending-{index}", agent_id=aid, state="deferred"))
    facade.supervisor.stages.planner.provider = lambda *args: pytest.fail("Learning backlog blocks new stage calls")
    facade.supervisor.stages.advance(aid)
    state = facade.store.snapshot(aid)["objective_planning"][0]
    assert "backlog" in state["reason"] and not facade.store.snapshot(aid)["stages"]


@pytest.mark.parametrize("goal_state", ["queued", "active"])
def test_required_source_revocation_blocks_stage_without_duplicate_goal(agent_service, goal_state):
    from loom.autonomy.events import ingest
    from loom.autonomy.stages import runnable

    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)

    def source_plan(agent, payload):
        result = decision(agent, payload)
        result["stage"]["required_source_ids"] = ["code"]
        return result

    facade.supervisor.stages.planner.provider = source_plan
    facade.supervisor.stages.advance(aid)
    with facade.store.sessions.transaction() as db:
        goal = next(g for g in facade.store.rows(db, "agent_goals", aid) if g.get("stage_id"))
        goal["state"] = goal_state
        facade.store.put(db, "agent_goals", goal)
    ingest(
        facade.store,
        aid,
        dict(
            command_id="revoked",
            source_id="code",
            event_id="revoked",
            object_id="src",
            version=1,
            occurred_at=facade.store.now(),
            cursor="1",
            type="revoked",
            summary="Bound source revoked",
        ),
    )
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    assert snap["objective_planning"][0]["state"] == "blocked"
    assert len(snap["stages"]) == 1
    with facade.store.sessions.transaction() as db:
        assert not runnable(facade.store, db, snap, next(g for g in snap["goals"] if g.get("stage_id")))


@pytest.mark.parametrize("cached_block", [False, True])
def test_completed_source_changed_goal_enters_review_and_clears_scheduler_intervention(agent_service, cached_block):
    from loom.autonomy.blockers import Code, make
    from loom.autonomy.events import ingest
    from loom.autonomy.stages import signature

    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    with facade.store.sessions.transaction() as db:
        goal = next(g for g in snap["goals"] if g.get("stage_id"))
        goal.update(state="active", required_source_ids=["code"])
        facade.store.put(db, "agent_goals", goal)
    event = ingest(facade.store, aid, dict(
        command_id="changed", source_id="code", event_id="changed", object_id="src", version=1,
        occurred_at=facade.store.now(), cursor="1", type="changed", summary="Implementation changed during execution",
    ))
    snap = facade.store.snapshot(aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    assert goal["source_changed"] == event["id"]
    delivery(facade, aid, goal)
    if cached_block:
        snap = facade.store.snapshot(aid)
        with facade.store.sessions.transaction() as db:
            state = snap["objective_planning"][0]
            objective = snap["role"]["long_term_objectives"][0]
            facade.supervisor.stages.save(
                db, state, state="blocked", reason="Display wording can change",
                blocker=make(Code.SOURCE_CHANGED, state["id"], "Unrelated translated display text"),
                last_input=signature(snap, objective, snap["goals"], [], [], []),
            )
        # Simulate loading an already-stuck planning record without any new inputs.
        from loom.autonomy.supervisor import AgentSupervisor

        facade.supervisor = AgentSupervisor(facade.service, facade.store, facade.background)
    calls = []

    def review(agent, payload):
        calls.append(payload)
        assert payload["stage"]["state"] == "reviewing"
        assert payload["source_changes"] == [event]
        assert next(g for g in payload["existing_goals"] if g["id"] == goal["id"])["source_changed"] == event["id"]
        return decision(agent, payload)

    facade.supervisor.stages.planner.provider = review
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["scheduler"] = dict(state="intervention", manual_required=True, reason="Previous source blocker")
        facade.store.put(db, "agents", agent)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert len(calls) == 1
    assert snap["stages"][0]["state"] == "completed"
    assert snap["objective_planning"][0]["state"] == "ready"
    assert snap["scheduler"]["state"] == "idle"
    assert not snap["scheduler"]["manual_required"]
    assert next(g for g in snap["goals"] if g["id"] == goal["id"])["source_changed"] == event["id"]
    assert len(snap["goals"]) == 2
