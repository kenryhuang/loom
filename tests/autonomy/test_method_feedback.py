import json

from loom.autonomy.context import build_snapshot
from loom.autonomy.method_feedback import report
from loom.service.contracts import canonical


def test_offered_methods_record_non_adoption_and_unknown_cost_without_claiming_improvement(agent_service):
    _, facade, aid, _ = agent_service
    guide = facade.guidance(aid, dict(command_id="method", method_text="Verify with recorded evidence", scope="persistent"), method=True)
    goal = facade.store.add_goal(aid, "goal-method", dict(objective="Verify", acceptance=["checked"]))
    with facade.store.sessions.transaction() as db:
        work = dict(
            id="w",
            agent_id=aid,
            goal_id=goal["id"],
            role_revision=1,
            state="succeeded",
            objective="Verify",
            run_ids=["r"],
            outcome=dict(acceptance="passed"),
            created_at=facade.store.now(),
        )
        facade.store.put(db, "agent_work_packages", work)
    build_snapshot(facade.store, aid, goal=goal, run_id="r")
    first = report(facade.store, aid)
    assert first["records"][0]["state"] == "unknown"
    with facade.store.sessions.transaction() as db:
        db.execute(
            "INSERT INTO agent_memory_uses VALUES(?,?,?,?)",
            (
                "use",
                aid,
                "r",
                canonical(
                    dict(run_id="r", references=dict(method_version_id=guide["method_version_id"], tool_call_ids=["actual-call"]), action_refs_validated=True)
                ),
            ),
        )
    second = report(facade.store, aid)
    assert second["records"][0]["state"] == "used"
    assert second["records"][0]["action_ids"] == ["actual-call"]
    assert second["causal_effect"] == "unknown"
    assert "improved" not in json.dumps(second)


def test_induction_worker_admits_each_model_request_to_agent_budget(tmp_path):
    import threading
    from types import SimpleNamespace

    from loom.autonomy.budget import BudgetLedger
    from loom.autonomy.store import AgentStore
    from loom.service.skills import SessionSkills
    from tests.autonomy.helpers import role
    from tests.skills.helpers import Judge
    from tests.skills.test_induction import job, ready

    sessions, skills, _, catalog = ready(tmp_path)
    agents = AgentStore(sessions)
    configured = role(tmp_path)
    configured["budget"] = dict(daily_tokens=10000000, output_tokens=1000)
    agent = agents.create("method-agent", configured)
    agents.command(agent["id"], "enable", "enable", 1)
    work = job(skills, skills.all_cases(str(tmp_path)))
    work["agent_binding"] = dict(agent_id=agent["id"])
    skills.save_job(work)
    worker = object.__new__(SessionSkills)
    worker.store, worker.catalog = skills, catalog
    worker.closed = threading.Event()
    worker.cancels = {work["id"]: threading.Event()}
    worker.semantic = SimpleNamespace(provider_factory=lambda _model: Judge())
    worker._work(work["id"])
    assert skills.job(work["id"])["state"] == "completed"
    charges = BudgetLedger(sessions.path).summary(agent["id"])
    assert charges and all(r["category"] == "method_learning" for r in charges)
    assert sum(r["calls"] for r in charges) >= 4


def test_agent_budget_denial_retains_induction_checkpoint_without_fictitious_cost(tmp_path):
    import threading
    from types import SimpleNamespace

    from loom.autonomy.budget import BudgetLedger
    from loom.autonomy.store import AgentStore
    from loom.service.skills import SessionSkills
    from tests.autonomy.helpers import role
    from tests.skills.helpers import Judge
    from tests.skills.test_induction import job, ready

    sessions, skills, _, catalog = ready(tmp_path)
    agents = AgentStore(sessions)
    configured = role(tmp_path)
    configured["budget"] = dict(daily_tokens=1)
    agent = agents.create("deferred-method", configured)
    agents.command(agent["id"], "enable", "enable", 1)
    work = job(skills, skills.all_cases(str(tmp_path)))
    work["agent_binding"] = dict(agent_id=agent["id"])
    skills.save_job(work)
    worker = object.__new__(SessionSkills)
    worker.store, worker.catalog = skills, catalog
    worker.closed = threading.Event()
    worker.cancels = {work["id"]: threading.Event()}
    judge = Judge()
    worker.semantic = SimpleNamespace(provider_factory=lambda _model: judge)
    worker._work(work["id"])
    deferred = skills.job(work["id"])
    assert deferred["state"] == "deferred"
    assert deferred["usage"]["calls"] == deferred["usage"]["tokens"] == 0
    assert deferred["requests"][-1]["state"] == "not_sent"
    assert not judge.calls and not BudgetLedger(sessions.path).summary(agent["id"])
    current = agents.snapshot(agent["id"])
    updated = {k: v for k, v in current["role"].items() if k != "schema_version"}
    updated["budget"]["daily_tokens"] = 10000000
    agents.revise_role(agent["id"], "grant", current["revision"], updated)
    worker._work(work["id"])
    assert skills.job(work["id"])["state"] == "completed"
