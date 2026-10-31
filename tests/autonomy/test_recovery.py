import json
from types import SimpleNamespace

import pytest

from loom.autonomy.budget import BudgetLedger, BudgetProvider
from loom.autonomy.control import drain
from loom.autonomy.learning import LearningCoordinator
from loom.autonomy.store import AgentStore
from loom.autonomy.triggers import materialize
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.autonomy.helpers import Clock, role
from tests.autonomy.test_dispatch import prepare
from tests.autonomy.test_learning import terminal


def dispatched(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    return service, facade, aid, facade.snapshot(aid)["sessions"][0]["session_id"]


def test_pause_resume_and_cancel_survive_acknowledgement_loss(agent_service):
    service, facade, aid, sid = dispatched(agent_service)
    # Commit only the Agent command: simulate death before the Session control.
    receipt = facade.store.command(aid, "pause", "pause", 2)
    assert service.store.snapshot(sid)["task"]["state"] == "queued"
    drain(service, facade.store, aid)
    assert service.store.snapshot(sid)["task"]["state"] == "paused"
    assert facade.control(aid, dict(command_id="pause", type="pause", expected_revision=2)) == receipt
    facade.control(aid, dict(command_id="resume", type="resume", expected_revision=3))
    assert service.store.snapshot(sid)["task"]["state"] == "queued"
    goal = facade.snapshot(aid)["goals"][0]
    command = dict(command_id="cancel", type="cancel", expected_revision=goal["revision"])
    receipt = facade.goal_command(aid, goal["id"], command)
    assert facade.goal_command(aid, goal["id"], command) == receipt
    assert facade.snapshot(aid)["work_packages"][0]["state"] == "cancelled"
    assert not facade.snapshot(aid)["learning"]  # No execution run ever started.
    with service.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM agent_outbox WHERE kind='control' AND state='pending'").fetchone()


def test_restart_reconciles_terminal_without_duplicate_learning(agent_service):
    service, facade, aid, sid = dispatched(agent_service)
    terminal(service, sid)
    first = facade.snapshot(aid)["learning"][0]
    directory = service.store.directory
    service.close()
    restored = LoomService(directory)
    try:
        snapshot = restored.background.autonomy.snapshot(aid)
        assert [c["id"] for c in snapshot["learning"]] == [first["id"]]
        assert snapshot["learning"][0]["terminal_event_cursor"] == first["terminal_event_cursor"]
        assert restored.background.autonomy.supervisor.thread is None
    finally:
        restored.close()


def test_scheduled_windows_coalesce_and_never_duplicate_active_work(agent_service):
    service, facade, aid, path = agent_service
    clock = Clock()
    store = AgentStore(service.store, clock=clock)
    raw = role(path)
    raw["long_term_objectives"][0]["schedule"] = dict(every_seconds=60, acceptance=["Review implementation evidence"])
    other = store.create("scheduled", raw)
    aid = other["id"]
    store.command(aid, "scheduled-enable", "enable", 1)
    materialize(store, aid)
    materialize(store, aid)
    assert len(store.snapshot(aid)["goals"]) == 1
    clock.advance(3600)
    materialize(store, aid)
    assert len(store.snapshot(aid)["goals"]) == 1
    with store.sessions.transaction() as db:
        goal = store.rows(db, "agent_goals", aid)[0]
        goal.update(state="completed", revision=2)
        store.put(db, "agent_goals", goal)
    materialize(store, aid)
    goals = store.snapshot(aid)["goals"]
    assert len(goals) == 2
    assert goals[-1]["schedule_window"] == 60


@pytest.mark.asyncio
async def test_budget_refuses_provider_before_request_and_learning_has_separate_slice(agent_service):
    service, facade, aid, _ = agent_service

    class Provider:
        model = "fixture"
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("Request must not be sent")

    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["budget"].update(daily_tokens=100, work_tokens=1, learning_tokens=100)
        facade.store.put(db, "agents", agent)
    ledger = BudgetLedger(service.store.path)
    binding = dict(agent_id=aid, goal_id="g", work_package_id="w")
    ledger.reserve("learning-independent", binding, 20, "learning")
    provider = Provider()
    wrapped = BudgetProvider(provider, ledger, binding, output_tokens=200)
    result = await wrapped.chat([])
    assert not result.ok and result.error.code == "AGENT_BUDGET"
    assert provider.calls == 0


def test_learning_daily_budget_defers_then_resumes_same_evaluation(agent_service):
    _, facade, aid, _ = agent_service
    clock = Clock()
    facade.store.clock = clock
    job = dict(state="incomplete", error="AGENT_BUDGET: Agent daily token budget exhausted", checkpoint={})
    calls = []
    semantic = SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": True}, start=lambda *args: calls.append(args))
    background = SimpleNamespace(semantic=semantic, trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}))
    coordinator = LearningCoordinator(facade.store, background)
    cycle = dict(
        id="learning-budget", agent_id=aid, session_id="s", run_id="r", analysis_id="a", evaluation_id="e", state="running", coverage="none", memory_result={}
    )
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_learning_cycles", cycle)
    coordinator.advance(aid)
    deferred = facade.snapshot(aid)["learning"][0]
    assert deferred["state"] == "deferred" and deferred["retry_after"] > clock().isoformat()
    coordinator.advance(aid)
    assert calls == []
    clock.advance(86401)
    coordinator.advance(aid)
    assert calls == [("s", "a", {"resume_id": "e", "retry_failed": True})]
    assert facade.snapshot(aid)["learning"][0]["state"] == "running"


@pytest.mark.parametrize('scope', ['contract', 'goal', 'work'])
def test_learning_preserves_budget_values_and_never_retries_cumulative_limit_at_midnight(agent_service, scope):
    _, facade, aid, _ = agent_service
    message = (f'Agent {scope} token budget exhausted: limit=100, used=80, requested=25, exceeded_by=5; '
               'Agent daily token budget exhausted: limit=100, used=80, requested=25, exceeded_by=5')
    job = dict(state='incomplete', checkpoint={'stage': {'error': {'message': message}}})
    semantic = SimpleNamespace(_load=lambda *args: job, _public=lambda job: {'retryable_work': True})
    background = SimpleNamespace(semantic=semantic, trajectory=SimpleNamespace(get=lambda *args: {'state': 'completed'}))
    cycle = dict(id='budget-details', agent_id=aid, session_id='s', run_id='r', analysis_id='a', evaluation_id='e',
                 state='running', coverage='none', memory_result={})
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, 'agent_learning_cycles', cycle)
    LearningCoordinator(facade.store, background).advance(aid)
    result = facade.snapshot(aid)['learning'][0]
    assert result['state'] == 'failed'
    assert result['blocked_reason'] == message
    assert not result.get('auto_resume')


def test_learning_command_is_durable_idempotent_and_deletion_is_final(agent_service):
    service, facade, aid, sid = dispatched(agent_service)
    terminal(service, sid)
    cycle = facade.snapshot(aid)["learning"][0]
    body = dict(command_id="cancel-learning", type="cancel")
    receipt = facade.learning_command(aid, cycle["id"], body)
    assert facade.learning_command(aid, cycle["id"], body) == receipt
    facade.supervisor.learning.advance(aid)
    assert facade.snapshot(aid)["learning"][0]["requested_action"] is None
    with pytest.raises(ServiceError, match="different input"):
        facade.learning_command(aid, cycle["id"], {**body, "type": "retry"})
    cycle.update(analysis_id="deleted", state="running", requested_action=None)
    with service.store.transaction() as db:
        facade.store.put(db, "agent_learning_cycles", cycle)
    facade.supervisor.learning.advance(aid)
    assert facade.snapshot(aid)["learning"][0]["state"] == "failed"


def test_document_import_is_immutable_and_workspace_scoped(agent_service):
    service, facade, aid, path = agent_service
    (path / "METHOD.md").write_text("Check evidence before a conclusion")
    method = facade.guidance(aid, dict(command_id="import", document_path="METHOD.md", scope="persistent"), method=True)
    (path / "METHOD.md").write_text("Changed on disk")
    with service.store.transaction() as db:
        saved = json.loads(db.execute("SELECT body FROM skill_versions WHERE id=?", (method["method_version_id"],)).fetchone()[0])
    assert saved["method_text"] == "Check evidence before a conclusion"
    with pytest.raises(ServiceError, match="inside working_directory"):
        facade.guidance(aid, dict(command_id="bad-import", document_path="../outside.md", scope="persistent"), method=True)


def test_agent_resume_keeps_explicitly_paused_goal_paused(agent_service):
    service, facade, aid, sid = dispatched(agent_service)
    goal = facade.snapshot(aid)["goals"][0]
    facade.goal_command(aid, goal["id"], dict(command_id="goal-pause", type="pause", expected_revision=goal["revision"]))
    facade.control(aid, dict(command_id="pause", type="pause", expected_revision=2))
    facade.control(aid, dict(command_id="resume", type="resume", expected_revision=3))
    assert service.store.snapshot(sid)["task"]["state"] == "paused"


def test_manual_followup_does_not_reopen_agent_contract(agent_service):
    service, facade, aid, sid = dispatched(agent_service)
    terminal(service, sid)
    service.store.submit(sid, dict(command_id="human-followup", type="submit_message", payload={"content": "Independent manual task"}))
    assert not service.store.snapshot(sid).get("autonomy")
    assert facade.snapshot(aid)["work_packages"][0]["state"] == "succeeded"
    assert len(facade.snapshot(aid)["learning"]) == 1


def test_service_flag_prevents_queued_autonomous_admission(agent_service):
    import time

    service, facade, aid, sid = dispatched(agent_service)
    service.start()
    time.sleep(0.25)
    assert service.store.snapshot(sid)["task"]["state"] == "queued"
    assert service.active == {}
    assert facade.supervisor.thread is None


def test_manual_priority_and_autonomous_aging_preserve_workspace_arbitration():
    from datetime import UTC, datetime, timedelta

    from loom.service.scheduler import ready_sessions

    now = datetime.now(UTC)
    auto = dict(
        session_id="auto", autonomy={"agent_id": "a"}, created_at=now.isoformat(), queued_since=now.isoformat(), task={"state": "queued", "workspace": "/same"}
    )
    manual = dict(session_id="manual", task={"state": "queued", "workspace": "/same"})
    assert list(ready_sessions([manual, auto], {}, 2)) == ["manual"]
    auto["queued_since"] = (now - timedelta(seconds=301)).isoformat()
    assert list(ready_sessions([manual, auto], {}, 2)) == ["auto"]
