import pytest

from loom.autonomy.acceptance import criteria, refs, support, validate_completion
from loom.autonomy.blockers import Code, Recovery, from_exception, holds, make, manual_required
from loom.autonomy.migrations import migrate
from loom.autonomy.stage_planner import validate
from loom.autonomy.stages import evidence
from loom.service.contracts import ServiceError, canonical
from tests.autonomy.test_stage_contracts import delivery
from tests.autonomy.test_stage_end_to_end import decision
from tests.autonomy.test_stage_progression import setup_plan


def review_fixture():
    definitions = criteria("stage", ["Tests and build pass"])
    payload = dict(stage=dict(id="stage", acceptance_criteria=definitions, goal_ids=["goal"]),
                   objective=dict(mode="finite", stage_planning={}),
                   evidence=[dict(id="delivery:work", kind="delivery", goal_id="goal", passed=True,
                                  verified_criteria=refs(definitions), acceptance=["Tests and build pass"])])
    review = dict(action="review_stage", result="completed", reason="Verified", source_refs=["delivery:work"],
                  checks=[dict(refs(definitions)[0], source_refs=["delivery:work"])])
    return payload, review


def test_descriptions_and_error_messages_are_not_acceptance_keys():
    payload, review = review_fixture()
    payload["stage"]["acceptance_criteria"][0]["text"] = "任意翻译和展示调整"
    payload["evidence"][0]["acceptance"] = ["Entirely different display text"]
    review["reason"] = "budget Source changed 任意文本"
    assert validate(review, payload) == review


@pytest.mark.parametrize("change", ["revision", "id", "wrong_stage", "unpassed", "missing", "duplicate"])
def test_acceptance_requires_exact_identity_revision_scope_and_complete_coverage(change):
    payload, review = review_fixture()
    if change == "revision":
        payload["stage"]["acceptance_criteria"][0]["revision"] += 1
    elif change == "id":
        review["checks"][0]["criterion_id"] = "unknown"
    elif change == "wrong_stage":
        payload["evidence"][0]["goal_id"] = "another-stage-goal"
    elif change == "unpassed":
        payload["evidence"][0]["passed"] = False
    elif change == "missing":
        review["checks"] = []
    else:
        review["checks"] *= 2
    with pytest.raises(ServiceError) as error:
        validate(review, payload)
    assert error.value.code in {"ACCEPTANCE_COVERAGE", "ACCEPTANCE_EVIDENCE"}


def test_final_work_inherits_stage_ids_and_stale_run_receipt_cannot_pass(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    facade.supervisor._plan(snap)
    snap = facade.store.snapshot(aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    work = next(w for w in snap["work_packages"] if w["goal_id"] == goal["id"])
    wanted = {(c["id"], c["revision"]) for c in stage["acceptance_criteria"]}
    assert wanted <= {(c["id"], c["revision"]) for c in goal["acceptance_criteria"]}
    assert wanted <= {(c["id"], c["revision"]) for c in work["acceptance_criteria"]}
    delivery(facade, aid, goal)
    with facade.store.sessions.transaction() as db:
        delivered = facade.store.get(db, "agent_work_packages", "work-" + goal["id"])
        delivered["terminal_run_id"] = "different-run"
        facade.store.put(db, "agent_work_packages", delivered)
        records = evidence(facade.store, db, aid, snap["goals"])
    assert not any(e["passed"] for e in records if e["goal_id"] == goal["id"])


def test_gate_verification_is_not_lost_when_many_outputs_follow_the_gate(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    delivery(facade, aid, goal)
    with facade.store.sessions.transaction() as db:
        work = facade.store.get(db, "agent_work_packages", "work-" + goal["id"])
        for seq in range(3, 20):
            db.execute("INSERT INTO events VALUES(?,?,?)", (work["session_id"], seq, canonical(dict(
                seq=seq, type="tool.completed", run_id=work["terminal_run_id"], payload=dict(output="Later output")))))
        work["outcome"]["event_cursor"] = 19
        work["acceptance_result"]["event_cursor"] = 19
        facade.store.put(db, "agent_work_packages", work)
        records = evidence(facade.store, db, aid, snap["goals"])
    assert next(e for e in records if e["goal_id"] == goal["id"])["passed"]


@pytest.mark.parametrize("scope,recovery,manual", [("daily", Recovery.WAIT_BUDGET, False), ("objective", Recovery.ADJUST_BUDGET, True)])
def test_budget_policy_depends_on_typed_scope_not_message(scope, recovery, manual):
    error = ServiceError("完全不同的提示，没有预算关键字", code="AGENT_BUDGET")
    error.budget_block = dict(scopes=[dict(scope=scope)])
    blocker = from_exception(error, "objective", "inputs", "budget-version")
    assert blocker["code"] == Code.PLANNING_BUDGET and blocker["recovery"] == recovery
    assert manual_required(blocker) == manual
    assert holds(blocker, "changed-inputs", "budget-version")
    assert not holds(blocker, "inputs", "new-budget-version")
    fake = from_exception(RuntimeError("Agent daily token budget exhausted"), "objective", "inputs", "budget-version")
    assert fake["code"] == Code.RUNTIME_FAILURE


@pytest.mark.parametrize("paused", [False, True])
def test_structural_reconciliation_ignores_wording_and_preserves_manual_pause(agent_service, paused):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    with facade.store.sessions.transaction() as db:
        goal["source_changed"] = "changed-event"
        facade.store.put(db, "agent_goals", goal)
    facade.supervisor.stages.reconcile(aid)
    snap = facade.store.snapshot(aid)
    state = snap["objective_planning"][0]
    assert state["blocker"]["code"] == Code.SOURCE_CHANGED
    with facade.store.sessions.transaction() as db:
        state.update(reason="Budget text cannot change this classification", manual_pause=paused)
        state["blocker"]["message"] = "Translated arbitrary text"
        facade.store.put(db, "agent_objective_planning", state)
    delivery(facade, aid, goal)
    facade.supervisor.stages.reconcile(aid)
    state = facade.store.snapshot(aid)["objective_planning"][0]
    assert (state["blocker"] is not None) == paused
    assert state["state"] == ("blocked" if paused else "active")


def test_explicit_retry_is_once_per_command_and_unknown_usage_is_not_retried(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    delivery(facade, aid, next(g for g in snap["goals"] if g.get("stage_id")))
    calls = []

    def fail(*args):
        calls.append(True)
        raise RuntimeError("arbitrary failure")

    facade.supervisor.stages.planner.provider = fail
    facade.supervisor.stages.advance(aid)
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 1
    snap = facade.store.snapshot(aid)
    facade.store.command(aid, "retry-once", "wake", snap["revision"])
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 2
    facade.store.command(aid, "retry-once", "wake", snap["revision"])
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 2
    with facade.store.sessions.transaction() as db:
        state = facade.store.rows(db, "agent_objective_planning", aid)[0]
        state["blocker"] = make(Code.UNKNOWN_RESPONSE, state["id"], "cost unknown")
        facade.store.put(db, "agent_objective_planning", state)
    snap = facade.store.snapshot(aid)
    facade.store.command(aid, "cannot-retry-unknown", "wake", snap["revision"])
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 2


def test_legacy_migration_is_idempotent_preserves_history_and_does_not_guess_error_text(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    delivery(facade, aid, next(g for g in snap["goals"] if g.get("stage_id")))
    with facade.store.sessions.transaction() as db:
        revisions = db.execute("SELECT COUNT(*) FROM agent_revisions").fetchone()[0]
        for table in ("agents", "agent_stages", "agent_goals", "agent_work_packages", "agent_objective_planning"):
            for record in facade.store.rows(db, table, aid):
                for field in ("objective_contracts", "acceptance_criteria", "acceptance_result", "blocker", "retry_requested"):
                    record.pop(field, None)
                if table == "agent_objective_planning":
                    record.update(state="blocked", reason="Required source changed; review or replace the affected contract")
                db.execute(f"UPDATE {table} SET body=? WHERE id=?", (canonical(record), record["id"]))
        db.execute("DELETE FROM autonomy_migrations WHERE version=5")
        migrate(db)
        migrated = {t: facade.store.rows(db, t, aid) for t in ("agents", "agent_stages", "agent_goals", "agent_work_packages")}
        state = facade.store.rows(db, "agent_objective_planning", aid)[0]
        assert state["blocker"]["code"] == Code.LEGACY_UNCLASSIFIED
        assert state["state"] == "blocked"
        stage = migrated["agent_stages"][0]
        records = evidence(facade.store, db, aid, migrated["agent_goals"])
        payload = dict(stage=stage, evidence=records, objective={})
        assert all(c["source_refs"] for c in support(payload))
        validate_completion(decision(None, payload), payload)
        migrate(db)
        assert migrated == {t: facade.store.rows(db, t, aid) for t in migrated}
        assert db.execute("SELECT COUNT(*) FROM agent_revisions").fetchone()[0] == revisions


@pytest.mark.parametrize("typed", [False, True])
def test_execution_recovery_uses_stop_code_and_ignores_stop_message(agent_service, typed):
    from tests.autonomy.test_queue_scheduling import paused_work

    service, facade, aid, _, sid = paused_work(agent_service, "任意停止说明" if typed else "Active time budget exceeded",
                                               stop_code="ACTIVE_TIME_BUDGET_EXCEEDED" if typed else "UNKNOWN")
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == ("recovered" if typed else "intervention")
    assert service.snapshot(sid)["task"]["state"] == ("queued" if typed else "paused")


def test_daily_planning_budget_wait_is_not_reported_as_human_intervention(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    delivery(facade, aid, next(g for g in snap["goals"] if g.get("stage_id")))
    calls = []

    def budget(agent, payload):
        calls.append(payload)
        if len(calls) > 1:
            return decision(agent, payload)
        error = ServiceError("Localized daily limit", code="AGENT_BUDGET")
        error.budget_block = dict(scopes=[dict(scope="daily")])
        raise error

    facade.supervisor.stages.planner.provider = budget
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "waiting" and not snap["scheduler"]["manual_required"]
    assert snap["scheduler"]["blockers"][0]["code"] == Code.PLANNING_BUDGET
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(calls) == 1
    facade.store.clock.advance(86400)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(calls) == 2
    assert facade.store.snapshot(aid)["stages"][0]["state"] == "completed"


@pytest.mark.parametrize("recorded", [False, True])
def test_legacy_pending_planning_cannot_restart_without_explicit_recovery(agent_service, recorded):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    with facade.store.sessions.transaction() as db:
        run = facade.store.rows(db, "agent_runs", aid)[-1]
        run.update(state="recoverable", decision={"action": "legacy"} if recorded else None)
        facade.store.put(db, "agent_runs", run)
        agent = facade.store.get(db, "agents", aid)
        agent["planner_lease"] = run["id"]
        facade.store.put(db, "agents", agent)
        db.execute("DELETE FROM autonomy_migrations WHERE version=5")
        migrate(db)
        state = facade.store.rows(db, "agent_objective_planning", aid)[0]
        expected = Code.LEGACY_UNCLASSIFIED if recorded else Code.UNKNOWN_RESPONSE
        assert state["state"] == "blocked" and state["blocker"]["code"] == expected
        assert not facade.store.get(db, "agents", aid)["planner_lease"]
        assert facade.store.get(db, "agent_runs", run["id"])["legacy_decision"] == run["decision"]
    assert snap["stages"]


def test_migration_keeps_completed_objective_closed(agent_service):
    from loom.autonomy.store import digest

    _, facade, aid, _ = agent_service
    setup_plan(facade, aid)
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        state = facade.store.rows(db, "agent_objective_planning", aid)[0]
        state.update(state="completed", objective_contract=digest(agent["role"]["long_term_objectives"][0]))
        facade.store.put(db, "agent_objective_planning", state)
        db.execute("DELETE FROM autonomy_migrations WHERE version=5")
        migrate(db)
    calls = []
    facade.supervisor.stages.planner.provider = lambda *args: calls.append(args)
    facade.supervisor.stages.advance(aid)
    assert not calls
    assert facade.store.snapshot(aid)["objective_planning"][0]["state"] == "completed"
