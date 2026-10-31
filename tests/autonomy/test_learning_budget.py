from copy import deepcopy
from types import SimpleNamespace

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.learning import LearningCoordinator
from loom.autonomy.learning_budget import admit_resume, current_block
from loom.autonomy.store import AgentStore
from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from tests.autonomy.helpers import Clock, role


def setup(tmp_path, *, policy=None, budget=None):
    clock = Clock()
    store = AgentStore(SessionStore(tmp_path / "data"), clock=clock)
    agent = store.create("create", {**role(tmp_path), "budget": {"daily_tokens":1000, "goal_tokens":1000, "learning_tokens":100, **(budget or {})},
                                    **({"learning_policy": policy} if policy else {})})
    aid = agent["id"]
    store.command(aid, "enable", "enable", 1)
    cycle = dict(id="cycle", agent_id=aid, goal_id="goal", work_package_id="work", evaluation_id="evaluation", session_id="session",
                 analysis_id="analysis", run_id="run", state="running", coverage="none", memory_result={}, attempts=1)
    with store.sessions.transaction() as db:
        store.put(db, "agent_learning_cycles", cycle)
    binding = {k: cycle[k] for k in ("agent_id", "goal_id", "work_package_id", "evaluation_id")}
    ledger = BudgetLedger(store.sessions.path, clock=clock)
    job = dict(id="evaluation", state="failed", autonomy_budget=binding, checkpoint={"cursor": 1, "memory": {"cursor": 1, "records": {}}})
    ledger.reserve("used", binding, 80, "learning")
    ledger.settle("used", 80)
    return store, clock, ledger, cycle, job


def deny(store, ledger, job, tokens=30, identifier="denial"):
    with pytest.raises(ServiceError) as error:
        ledger.reserve(identifier, job["autonomy_budget"], tokens, "learning")
    job.update(state="failed", error=str(error.value), error_details=[{"provider_code": "AGENT_BUDGET", "reason": str(error.value)}])
    return current_block(store, job)


def test_expansion_is_daily_allocation_and_replays_without_changing_contract(tmp_path):
    store, clock, ledger, cycle, job = setup(tmp_path)
    block = deny(store, ledger, job)
    first = admit_resume(store, cycle["agent_id"], cycle, job, block)
    assert first["outcome"] == "resume" and first["resume"]["after"] == 200
    restarted = AgentStore(store.sessions, clock=clock)
    assert admit_resume(restarted, cycle["agent_id"], cycle, job, block) == first
    snapshot = store.snapshot(cycle["agent_id"])
    assert snapshot["role"]["budget"]["learning_tokens"] == 100
    assert len(snapshot["learning_allocations"][0]["grants"]) == 1
    ledger.reserve("resumed", job["autonomy_budget"], 30, "learning")
    ledger.settle("resumed", 30)
    assert ledger.summary(cycle["agent_id"])[0]["tokens"] == 110
    clock.advance(86400)
    assert ledger.learning_summary(cycle["agent_id"])["effective_tokens"] == 100


@pytest.mark.parametrize("scope,outcome", [("daily_tokens", "wait"), ("goal_tokens", "stop"), ("max_calls", "wait")])
def test_hard_parent_limits_are_not_expanded(tmp_path, scope, outcome):
    store, _, ledger, cycle, job = setup(tmp_path)
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", cycle["agent_id"])
        agent["role"]["budget"][scope] = 1 if scope == "max_calls" else 100
        store.put(db, "agents", agent)
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    assert decision["outcome"] == outcome
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


@pytest.mark.parametrize("policy,outcome", [({"auto_expand": False}, "wait"), ({"max_expansions": 0}, "wait"),
                                           ({"max_learning_tokens": 100}, "wait"), ({"max_resumes": 0}, "stop")])
def test_configured_expansion_and_resume_limits_are_enforced(tmp_path, policy, outcome):
    store, _, ledger, cycle, job = setup(tmp_path, policy=policy)
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    assert decision["outcome"] == outcome
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


def test_no_progress_stops_new_denial_and_progress_allows_second_bounded_grant(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    block = deny(store, ledger, job, 130, "second-denial")
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["reason"].startswith("LEARNING_NO_PROGRESS")
    job["checkpoint"]["memory"]["cursor"] = 2
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["reason"].startswith("LEARNING_NO_PROGRESS")
    job["checkpoint"]["memory"]["units"] = {"finished-1": {"complete": True}}
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["resume"]["after"] == 400
    job["checkpoint"]["memory"]["units"]["finished-2"] = {"complete": True}
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job, 330, "third-denial"))
    assert decision["outcome"] == "wait"
    assert len(store.snapshot(cycle["agent_id"])["learning_allocations"][0]["grants"]) == 2


def test_legacy_typed_denial_is_migrated_but_free_text_is_not(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    deny(store, ledger, job)
    with store.sessions.transaction() as db:
        db.execute("DELETE FROM agent_budget_blocks")
    block = current_block(store, job)
    assert block["origin"] == "legacy_typed_budget_issue" and block["requested"] == 30
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["outcome"] == "resume"
    job["error_details"] = []
    assert current_block(store, job) is None


def test_coordinator_resumes_existing_evaluation_without_retrying_settled_quality_gaps(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    deny(store, ledger, job)
    cycle.update(state="deferred", auto_resume=True, retry_after="2099-01-01")
    with store.sessions.transaction() as db:
        store.put(db, "agent_learning_cycles", cycle)
    starts = []

    def start(sid, analysis_id, options, **kwargs):
        starts.append((sid, analysis_id, options, kwargs))
        job.update(state="queued", host_resume_requests=[kwargs["resume_request_id"]])

    background = SimpleNamespace(trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": True}, start=start))
    coordinator = LearningCoordinator(store, background)
    coordinator.advance(cycle["agent_id"])
    coordinator.advance(cycle["agent_id"])
    assert len(starts) == 1
    assert starts[0][2] == {"resume_id": "evaluation"}
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "running"


def test_resume_intent_survives_lost_acknowledgement_after_evaluation_finishes(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    admitted = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    job.update(state="completed", host_resume_requests=[admitted["resume"]["id"]])
    background = SimpleNamespace(trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": False},
                                 start=lambda *args, **kwargs: pytest.fail("Completed evaluation must not be restarted")))
    LearningCoordinator(store, background).advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "completed" and saved["budget_resume_pending"] is None


def test_cancel_between_denial_and_allocation_cannot_grant_or_resume(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    block = deny(store, ledger, job)
    with store.sessions.transaction() as db:
        current = deepcopy(cycle)
        current.update(command_generation=1, requested_action="cancel")
        store.put(db, "agent_learning_cycles", current)
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["outcome"] == "hold"
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


def test_stale_budget_denial_does_not_expand_for_new_pause_error(tmp_path):
    store, _, ledger, _, job = setup(tmp_path)
    deny(store, ledger, job)
    job['error_details'] = [{'provider_code': 'AGENT_BUDGET', 'reason': 'Agent is paused; new calls are deferred'}]
    assert current_block(store, job) is None


def test_unknown_usage_reservations_remain_charged_after_expansion(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    ledger.reserve('unknown', job['autonomy_budget'], 15, 'learning')
    ledger.settle('unknown', None)
    block = deny(store, ledger, job)
    admit_resume(store, cycle['agent_id'], cycle, job, block)
    assert store.snapshot(cycle['agent_id'])['learning_allocations'][0]['grants'][0]['used'] == 95
    ledger.reserve('fits', job['autonomy_budget'], 105, 'learning')
    with pytest.raises(ServiceError, match='limit=200, used=200'):
        ledger.reserve('over', job['autonomy_budget'], 1, 'learning')


@pytest.mark.parametrize('policy', [{'auto_expand': 'yes'}, {'max_resumes': True}, {'max_expansions': 11}, {'max_learning_tokens': 0}])
def test_learning_policy_rejects_invalid_bounds(tmp_path, policy):
    with pytest.raises(ServiceError):
        setup(tmp_path, policy=policy)
