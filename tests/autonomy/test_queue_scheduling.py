import asyncio
import threading
from pathlib import Path

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.contracts import role_contract, scheduling_policy
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.autonomy.helpers import Clock, EvidenceSolver, evaluation_provider, goals, role, single_package
from tests.autonomy.test_dispatch import prepare
from tests.autonomy.test_execution_flow import until
from tests.autonomy.test_learning import terminal


class WaitingSolver(EvidenceSolver):
    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        root = Path(self.state["task"]["workspace"])
        (root / "pending").touch()
        for _ in range(300):
            if (root / "release").exists():
                break
            await asyncio.sleep(0.03)
        return await super().chat(messages, tools, cancellation, tool_choice)


def waiting_provider(state):
    return WaitingSolver(state)


def test_running_worker_is_never_preempted_by_timer_or_manual_check(tmp_path):
    (tmp_path / "notes.txt").write_text("Session fixture evidence: queue test")
    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=waiting_provider,
        agent_planner=single_package,
        evaluation_provider_factory=evaluation_provider,
    )
    try:
        facade = service.background.autonomy
        facade.supervisor.start = lambda: None
        clock = Clock()
        facade.store.clock = clock
        aid = facade.store.create("create", role(tmp_path))["id"]
        facade.store.add_goal(aid, "goal", goals()[0])
        facade.store.command(aid, "enable", "enable", 1)
        service.start()
        facade.supervisor.scheduler.advance(aid)
        until(lambda: (tmp_path / "pending").exists(), bool)
        work = facade.snapshot(aid)["work_packages"][0]
        sid = work["session_id"]
        run_id = service.snapshot(sid)["run"]["id"]
        clock.advance(7200)
        facade.supervisor.scheduler.advance(aid)
        facade.supervisor.scheduler.advance(aid, manual=True)
        state = service.store.snapshot(sid)
        assert state["task"]["limits"]["max_duration_seconds"] == 3600
        assert not state.get("control") and state["run"]["id"] == run_id
        assert facade.snapshot(aid)["scheduler"]["state"] == "running"
        assert len(facade.snapshot(aid)["work_packages"]) == 1
        # A checkpoint parked by the retired time-slice scheduler is still recoverable.
        pause_id = "slice-pause:legacy"
        with service.store.transaction() as db:
            current = facade.store.get(db, "agent_work_packages", work["id"], aid)
            current["slice"] = dict(phase="paused", number=1, seconds=300, pause_command_id=pause_id, run_id=run_id)
            facade.store.put(db, "agent_work_packages", current)
        service.submit(sid, dict(command_id=pause_id, type="pause", payload={}))
        (tmp_path / "release").touch()
        until(lambda: service.store.snapshot(sid), lambda s: s["task"]["state"] == "paused" and sid not in service.active)
        assert service.store.snapshot(sid)["run"]["checkpoint"]
        facade.supervisor.scheduler.advance(aid, manual=True)
        assert facade.snapshot(aid)["scheduler"]["state"] == "recovered"
        until(lambda: facade.snapshot(aid)["work_packages"][0]["state"], lambda s: s == "succeeded")
        assert facade.snapshot(aid)["work_packages"][0]["run_ids"] == [run_id]
    finally:
        (tmp_path / "release").touch()
        service.close()


def paused_work(agent_service, reason="Active time budget exceeded", control=None, *, stop_code=None):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sid = facade.snapshot(aid)["work_packages"][0]["session_id"]

    def pause(state, emit, db):
        state["run"] = dict(id="r1", state="suspended", reason=reason, active_seconds=3601, steps=2, checkpoint={"saved": True},
                            stop_code=stop_code or ("ACTIVE_TIME_BUDGET_EXCEEDED" if reason == "Active time budget exceeded" else None))
        state["task"]["state"] = "paused"
        state["control"] = control

    service.store.update(sid, pause)
    return service, facade, aid, work, sid


def test_historical_task_token_stop_resumes_under_shared_daily_budget_once(agent_service):
    service, facade, aid, work, sid = paused_work(agent_service, stop_code="TOKEN_BUDGET_EXCEEDED")
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["budget"].update(work_tokens=100, goal_tokens=10000, daily_tokens=10000)
        agent["role"]["scheduling_policy"]["max_work_tokens"] = 1000
        facade.store.put(db, "agents", agent)
    ledger = BudgetLedger(service.store.path)
    binding = dict(agent_id=aid, goal_id=work["goal_id"], work_package_id=work["id"], run_id="r1")
    ledger.reserve("old", binding, 90, "execution")
    ledger.settle("old", 90)
    facade.supervisor.scheduler.advance(aid, manual=True)
    saved = facade.snapshot(aid)
    assert saved["scheduler"]["state"] == "recovered", saved["scheduler"]
    assert saved["work_packages"][0]["execution_budget"]["tokens"] is None
    assert saved["role"]["budget"]["work_tokens"] == 100
    assert service.snapshot(sid)["task"]["state"] == "queued"
    assert service.snapshot(sid)["run"]["id"] == "r1"
    assert service.snapshot(sid)["task"]["limits"]["max_duration_seconds"] == 3600
    ledger.reserve("next", binding, 50, "execution")
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)["work_packages"][0]["recovery"]["attempts"] == 1
    assert sum(x["tokens"] for x in ledger.summary(aid)) == 140


def test_lost_resume_ack_reconciles_without_another_recovery_attempt(agent_service, monkeypatch):
    service, facade, aid, _, sid = paused_work(agent_service)
    submit = service.submit

    def lost_ack(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(service, "submit", lost_ack)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)["scheduler"]["manual_required"]
    monkeypatch.setattr(service, "submit", submit)
    facade.supervisor.scheduler.advance(aid, manual=True)
    work = facade.snapshot(aid)["work_packages"][0]
    assert work["recovery"]["attempts"] == 1 and work["recovery"]["pending"] is None
    assert service.snapshot(sid)["task"]["state"] == "queued"


def test_daily_budget_waits_without_allocating_or_consuming_recovery(agent_service):
    service, facade, aid, work, sid = paused_work(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["budget"].update(work_tokens=100, goal_tokens=100, daily_tokens=100)
        facade.store.put(db, "agents", agent)
    ledger = BudgetLedger(service.store.path)
    binding = dict(agent_id=aid, goal_id=work["goal_id"], work_package_id=work["id"], run_id="r1")
    ledger.reserve("used", binding, 90, "execution")
    with pytest.raises(ServiceError):
        ledger.reserve("denied", binding, 50, "execution")
    service.store.update(sid, lambda s, e, db: s["run"].update(reason=None, failure=dict(code="AGENT_BUDGET", message="Typed admission denial")))
    facade.supervisor.scheduler.advance(aid, manual=True)
    saved = facade.snapshot(aid)
    assert not saved["scheduler"]["manual_required"] and "每日 Token 总预算不足" in saved["scheduler"]["reason"]
    assert saved["role"]["budget"]["goal_tokens"] == 100
    assert not saved["work_packages"][0].get("recovery", {}).get("attempts")
    assert saved['scheduler']['blockers'][0]['code'] == 'EXECUTION_BUDGET'
    assert service.snapshot(sid)["task"]["state"] == "paused"


@pytest.mark.parametrize("case", ["manual", "input", "limit", "unknown"])
def test_unrecoverable_work_requires_manual_intervention(agent_service, case):
    control = dict(command_id="human", kind="paused") if case == "manual" else None
    service, facade, aid, work, sid = paused_work(agent_service, "Unknown tool failure" if case == "unknown" else "Active time budget exceeded", control)
    with service.store.transaction() as db:
        if case == "limit":
            agent = facade.store.get(db, "agents", aid)
            agent["role"]["scheduling_policy"]["max_recovery_attempts"] = 0
            facade.store.put(db, "agents", agent)
    if case == "input":
        service.store.update(sid, lambda s, e, db: s.update(input_request=dict(id="question", state="pending", question="Choose a design")))
    facade.supervisor.scheduler.advance(aid, manual=True)
    result = facade.snapshot(aid)
    assert result["scheduler"]["manual_required"] and result["scheduler"]["next_steps"]
    assert service.snapshot(sid)["task"]["state"] == "paused"
    assert len(result["work_packages"]) == 1


def test_timer_interval_and_manual_single_flight_do_not_invalidate_planner(agent_service):
    _, facade, aid, _ = agent_service
    clock = Clock()
    facade.store.clock = clock
    entered, release = threading.Event(), threading.Event()

    def planner(agent, goal, context):
        entered.set()
        assert release.wait(10)
        return single_package(agent, goal, context)

    facade.supervisor.planner.provider = planner
    facade.store.add_goal(aid, "goal", goals()[0])
    thread = threading.Thread(target=lambda: facade.supervisor.scheduler.advance(aid, manual=True))
    thread.start()
    try:
        assert entered.wait(10)
        before = facade.snapshot(aid)
        assert facade.supervisor.scheduler.advance(aid, manual=True) is False
        facade.control(aid, dict(command_id="manual", type="wake", expected_revision=before["revision"]))
        during = facade.snapshot(aid)
        assert during["planner_lease"] == before["planner_lease"]
        assert during["generation"] == before["generation"]
    finally:
        release.set()
        thread.join(10)
    assert len(facade.snapshot(aid)["work_packages"]) == 1
    assert facade.supervisor.scheduler.advance(aid) is False
    assert facade.supervisor.scheduler.advance(aid, manual=True) is True


def test_empty_queue_plans_new_goal_and_dispatches_in_same_check(agent_service):
    service, facade, aid, _ = agent_service
    first = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, first["id"])
    terminal(service, facade.snapshot(aid)["sessions"][0]["session_id"])
    called = []

    def plan_stage(identifier):
        called.append(identifier)
        facade.store.add_goal(aid, "next-goal", goals()[1])

    facade.supervisor.stages.advance = plan_stage
    facade.supervisor.scheduler.advance(aid, manual=True)
    saved = facade.snapshot(aid)
    assert called == [aid] and len(saved["work_packages"]) == 2
    assert saved["work_packages"][-1]["state"] == "dispatched"
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert called == [aid]


def test_new_policy_defaults_and_legacy_slice_fields_are_migrated(tmp_path):
    raw = role(tmp_path)
    raw.pop("scheduling_policy")
    configured = role_contract(raw)
    assert configured["scheduling_policy"]["enabled"] is True
    assert configured["scheduling_policy"]["tick_seconds"] == 60
    policy = scheduling_policy({**configured, "scheduling_policy": dict(enabled=True, tick_seconds=300, max_slice_seconds=900)})
    assert policy["tick_seconds"] == 300 and "max_slice_seconds" not in policy
