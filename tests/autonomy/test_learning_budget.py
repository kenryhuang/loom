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
    agent = store.create(
        "create",
        {
            **role(tmp_path),
            "budget": {"daily_tokens": 1000, "goal_tokens": 1000, "learning_tokens": 100, **(budget or {})},
            **({"learning_policy": policy} if policy else {}),
        },
    )
    aid = agent["id"]
    store.command(aid, "enable", "enable", 1)
    cycle = dict(
        id="cycle",
        agent_id=aid,
        goal_id="goal",
        work_package_id="work",
        evaluation_id="evaluation",
        session_id="session",
        analysis_id="analysis",
        run_id="run",
        state="running",
        coverage="none",
        memory_result={},
        attempts=1,
    )
    with store.sessions.transaction() as db:
        store.put(db, "agent_learning_cycles", cycle)
    binding = {k: cycle[k] for k in ("agent_id", "goal_id", "work_package_id", "evaluation_id")}
    ledger = BudgetLedger(store.sessions.path, clock=clock)
    job = dict(id="evaluation", state="failed", autonomy_budget=binding, checkpoint={"cursor": 1, "memory": {"cursor": 1, "records": {}}})
    ledger.reserve("used", binding, 80, "learning")
    ledger.settle("used", 80)
    return store, clock, ledger, cycle, job


def deny(store, ledger, job, tokens=30, identifier="denial"):
    # Reproduce a persisted learning-subquota denial from the retired policy.
    from loom.service.contracts import canonical

    aid = job['autonomy_budget']['agent_id']
    with store.sessions.transaction() as db:
        window = store.now()[:10]
        actual, reserved = db.execute(
            "SELECT COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0) "
            "FROM agent_budget_calls WHERE agent_id=?", (aid,),
        ).fetchone()
        used = actual + reserved
        message = (f'Agent learning token budget exhausted: limit=100, used={used} (actual={actual}, reserved_or_unknown={reserved}), '
                   f'requested={tokens}, projected={used + tokens}, remaining={max(0,100-used)}, exceeded_by={used + tokens - 100} tokens; '
                   f'window={window}, timezone=UTC, category=learning; increase learning_tokens')
        block = dict(id=identifier, schema_version=1, binding=job['autonomy_budget'], category='learning', window=window,
                     requested=tokens, reason=message, scopes=[dict(scope='learning', used=used, limit=100, requested=tokens)])
        db.execute('INSERT INTO agent_budget_blocks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                   (job['id'], aid, canonical(block)))
    job.update(state='failed', error=message, error_details=[dict(provider_code='AGENT_BUDGET', reason=message)])

    return current_block(store, job)


def test_historical_learning_stop_resumes_without_suballocation_and_replays_once(tmp_path):
    store, clock, ledger, cycle, job = setup(tmp_path)
    block = deny(store, ledger, job)
    first = admit_resume(store, cycle["agent_id"], cycle, job, block)
    assert first["outcome"] == "resume" and first["resume"]["after"] == 1000
    restarted = AgentStore(store.sessions, clock=clock)
    assert admit_resume(restarted, cycle["agent_id"], cycle, job, block) == first
    snapshot = store.snapshot(cycle["agent_id"])
    assert snapshot["role"]["budget"]["learning_tokens"] == 100
    assert not snapshot["learning_allocations"]
    ledger.reserve("resumed", job["autonomy_budget"], 30, "learning")
    ledger.settle("resumed", 30)
    assert ledger.summary(cycle["agent_id"])[0]["tokens"] == 110
    clock.advance(86400)
    assert ledger.learning_summary(cycle['agent_id'])['effective_tokens'] is None


@pytest.mark.parametrize("scope,outcome", [("daily_tokens", "wait"), ("goal_tokens", "resume"), ("max_calls", "resume")])
def test_only_daily_parent_limit_gates_learning(tmp_path, scope, outcome):
    store, _, ledger, cycle, job = setup(tmp_path)
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", cycle["agent_id"])
        agent["role"]["budget"][scope] = 1 if scope == "max_calls" else 100
        store.put(db, "agents", agent)
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    assert decision["outcome"] == outcome
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


def test_historical_objective_limit_does_not_gate_learning(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", cycle["agent_id"])
        agent["role"]["long_term_objectives"][0]["stage_planning"] = dict(mode="autonomous", objective_tokens=100)
        store.put(db, "agents", agent)
        store.put(db, "agent_goals", dict(id="goal", agent_id=agent["id"], long_term_objective_id="quality", state="active"))
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    assert decision["outcome"] == "resume"
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


@pytest.mark.parametrize(
    "policy,outcome", [({"auto_expand": False}, "resume"), ({"max_expansions": 0}, "resume"),
                        ({"max_learning_tokens": 100}, "resume"), ({"max_resumes": 0}, "stop")]
)
def test_legacy_expansion_fields_are_ignored_but_resume_guard_remains(tmp_path, policy, outcome):
    store, _, ledger, cycle, job = setup(tmp_path, policy=policy)
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    assert decision["outcome"] == outcome
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


def test_no_progress_stops_retries_and_completed_progress_permits_resume(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    block = deny(store, ledger, job, 130, "second-denial")
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["reason"].startswith("LEARNING_NO_PROGRESS")
    job["checkpoint"]["memory"]["cursor"] = 2
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["reason"].startswith("LEARNING_NO_PROGRESS")
    job["checkpoint"]["memory"]["units"] = {"finished-1": {"complete": True}}
    assert admit_resume(store, cycle["agent_id"], cycle, job, block)["resume"]["after"] == 1000
    job["checkpoint"]["memory"]["units"]["finished-2"] = {"complete": True}
    decision = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job, 330, "third-denial"))
    assert decision["outcome"] == "resume"
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]


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

    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": True}, start=start),
    )
    coordinator = LearningCoordinator(store, background)
    coordinator.advance(cycle["agent_id"])
    coordinator.advance(cycle["agent_id"])
    assert len(starts) == 1
    assert starts[0][2] == {"resume_id": "evaluation"}
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "running"


def test_completed_with_unknown_finishes_learning_without_promoting_coverage_or_retry(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    job.update(state='completed', settings={}, usage={}, error=None, error_details=[])
    starts = []
    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {'state': 'completed'}),
        semantic=SimpleNamespace(_load=lambda *args: job,
            _public=lambda _: {'state': 'completed', 'completion_kind': 'limited', 'result_state': 'partial', 'retryable_work': 0},
            start=lambda *args, **kwargs: starts.append(args)),
    )
    LearningCoordinator(store, background)._advance(store.snapshot(cycle['agent_id']), cycle)
    assert cycle['state'] == 'completed' and cycle['coverage'] == 'partial'
    assert 'unknown' in cycle['blocked_reason'] and not starts


def test_daily_wait_does_not_exhaust_retries_and_resumes_automatically_next_day(tmp_path):
    store, clock, ledger, cycle, job = setup(tmp_path, budget=dict(daily_tokens=100))
    deny(store, ledger, job)
    starts = []
    def start(sid, analysis_id, options, **kwargs):
        starts.append(kwargs)
        job.update(state='queued', host_resume_requests=[kwargs['resume_request_id']])
    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: dict(state='completed')),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: dict(retryable_work=True), start=start),
    )
    coordinator = LearningCoordinator(store, background)
    for _ in range(10):
        coordinator.advance(cycle['agent_id'])
    waiting = store.snapshot(cycle['agent_id'])['learning'][0]
    assert waiting['state'] == 'deferred' and not starts
    assert waiting['retry_after'] == ledger.next_window(cycle['agent_id'])
    clock.advance(86400)
    coordinator.advance(cycle['agent_id'])
    assert len(starts) == 1
    assert store.snapshot(cycle['agent_id'])['learning'][0]['state'] == 'running'


def test_daily_policy_upgrade_reassesses_old_quota_stop_without_rewriting_checkpoint(tmp_path):
    import json

    store, clock, ledger, cycle, job = setup(tmp_path)
    deny(store, ledger, job)
    cycle.update(state='incomplete', auto_resume=False, budget_decision=dict(outcome='stop'))
    with store.sessions.transaction() as db:
        store.put(db, 'agent_learning_cycles', cycle)
        db.execute('CREATE TABLE semantic_jobs(id TEXT PRIMARY KEY,body TEXT)')
        db.execute('INSERT INTO semantic_jobs VALUES(?,?)', (job['id'], json.dumps(job)))
        db.execute('DELETE FROM autonomy_migrations WHERE version=6')
    before = deepcopy(job['checkpoint'])
    AgentStore(store.sessions, clock=clock)
    saved = store.snapshot(cycle['agent_id'])['learning'][0]
    assert saved['state'] == 'pending' and saved['auto_resume']
    assert saved['attempts'] == cycle['attempts']
    with store.sessions.read() as db:
        assert json.loads(db.execute('SELECT body FROM semantic_jobs').fetchone()[0])['checkpoint'] == before


def test_resume_intent_survives_lost_acknowledgement_after_evaluation_finishes(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    admitted = admit_resume(store, cycle["agent_id"], cycle, job, deny(store, ledger, job))
    job.update(state="completed", host_resume_requests=[admitted["resume"]["id"]])
    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(
            _load=lambda *args: job,
            _public=lambda job: {"retryable_work": False},
            start=lambda *args, **kwargs: pytest.fail("Completed evaluation must not be restarted"),
        ),
    )
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
    job["error_details"] = [{"provider_code": "AGENT_BUDGET", "reason": "Agent is paused; new calls are deferred"}]
    assert current_block(store, job) is None


@pytest.mark.parametrize("provider_code", ["AGENT_CONTROL", "AGENT_BUDGET"])
def test_agent_control_pause_waits_then_resumes_same_job_without_expansion(tmp_path, provider_code):
    store, _, _, cycle, job = setup(tmp_path)
    job.update(state="failed", error_details=[dict(provider_code=provider_code, reason="Agent is paused; new calls are deferred")])
    calls = []

    def start(sid, analysis, options, **kwargs):
        calls.append(options)
        job.update(state="running", host_resume_requests=[kwargs["resume_request_id"]])

    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": False}, start=start),
    )
    coordinator = LearningCoordinator(store, background)
    store.command(cycle["agent_id"], "pause", "pause", 2)
    coordinator.advance(cycle["agent_id"])
    assert calls == []
    store.command(cycle["agent_id"], "resume", "resume", 3)
    coordinator.advance(cycle["agent_id"])
    assert calls == [{"resume_id": "evaluation"}]
    snapshot = store.snapshot(cycle["agent_id"])
    assert snapshot["learning"][0]["state"] == "running"
    assert snapshot["learning"][0]["attempts"] == 2
    assert snapshot["learning_allocations"] == []


@pytest.mark.asyncio
async def test_paused_provider_has_control_code_and_never_calls_model(tmp_path):
    from loom.autonomy.budget import BudgetProvider

    store, _, ledger, cycle, job = setup(tmp_path)
    store.command(cycle["agent_id"], "pause", "pause", 2)
    wrapped = BudgetProvider(
        SimpleNamespace(model="fixture", chat=lambda *args: pytest.fail("Paused provider cannot call model")),
        ledger,
        job["autonomy_budget"],
        output_tokens=1,
        category="learning",
    )
    result = await wrapped.chat([])
    assert not result.ok and result.error.code == "AGENT_CONTROL"


def test_unknown_usage_remains_charged_to_shared_daily_allowance(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path)
    ledger.reserve("unknown", job["autonomy_budget"], 15, "learning")
    ledger.settle("unknown", None)
    block = deny(store, ledger, job)
    admit_resume(store, cycle["agent_id"], cycle, job, block)
    assert not store.snapshot(cycle["agent_id"])["learning_allocations"]
    ledger.reserve("fits", job["autonomy_budget"], 905, "learning")
    with pytest.raises(ServiceError, match="limit=1000, used=1000"):
        ledger.reserve("over", job["autonomy_budget"], 1, "learning")


@pytest.mark.parametrize("policy", [{"auto_expand": "yes"}, {"max_resumes": True}, {"max_expansions": 11}, {"max_learning_tokens": 0}])
def test_learning_policy_rejects_invalid_bounds(tmp_path, policy):
    with pytest.raises(ServiceError):
        setup(tmp_path, policy=policy)


def test_evaluation_call_limit_expands_same_job_then_completes(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    job.update(state="budget_exhausted", settings={"max_calls": 48, "max_seconds": 3600}, usage={"calls": 48}, elapsed_seconds=10)
    calls = []

    def start(sid, analysis_id, options, **kwargs):
        calls.append((options, kwargs))
        job.update(state="queued", host_resume_requests=[kwargs["resume_request_id"]])

    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": False}, start=start),
    )
    coordinator = LearningCoordinator(store, background)
    coordinator.advance(cycle["agent_id"])
    coordinator.advance(cycle["agent_id"])
    assert len(calls) == 1 and calls[0][0] == {"resume_id": "evaluation", "max_calls": 96}
    job.update(state="completed")
    coordinator.advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "completed" and saved["coverage"] == "full" and saved["attempts"] == 2


def test_incomplete_is_retried_but_never_misreported_as_completed(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    job.update(state="incomplete")
    calls = []

    def start(sid, analysis_id, options, **kwargs):
        calls.append(options)
        job.setdefault("host_resume_requests", []).append(kwargs["resume_request_id"])

    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": True}, start=start),
    )
    coordinator = LearningCoordinator(store, background)
    coordinator.advance(cycle["agent_id"])
    assert calls == [{"resume_id": "evaluation", "retry_failed": True}]
    coordinator.advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "incomplete" and saved["coverage"] == "partial"
    assert "no new completed units" in saved["blocked_reason"]
    coordinator.advance(cycle["agent_id"])
    assert len(calls) == 1


def test_legacy_partial_cycle_keeps_polling_an_already_running_job(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    with store.sessions.transaction() as db:
        cycle.update(state="completed", coverage="partial", completion_policy_seen=1)
        store.put(db, "agent_learning_cycles", cycle)
    job.update(state="running")
    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda *args: job, _public=lambda job: {"retryable_work": False}),
    )
    coordinator = LearningCoordinator(store, background)
    coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "running"
    job.update(state="completed")
    coordinator.advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "completed" and saved["coverage"] == "full"


@pytest.mark.parametrize("policy,reason", [({"max_attempts": 1}, "attempt limit"), ({"max_evaluation_calls": 48}, "ceiling")])
def test_analysis_retry_hard_limits_retain_source_and_no_new_calls(tmp_path, policy, reason):
    store, _, _, cycle, job = setup(tmp_path, policy=policy)
    job.update(state="budget_exhausted", settings={"max_calls": 48}, usage={"calls": 48})
    background = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(
            _load=lambda *args: job, _public=lambda job: {"retryable_work": False}, start=lambda *args, **kwargs: pytest.fail("Limit cannot be bypassed")
        ),
    )
    LearningCoordinator(store, background).advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "incomplete" and reason in saved["blocked_reason"]
    assert saved["evaluation_id"] == cycle["evaluation_id"]
    assert saved["run_id"] == cycle["run_id"]
