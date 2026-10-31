from copy import deepcopy
from types import SimpleNamespace

from loom.autonomy.learning import LearningCoordinator
from loom.autonomy.learning_queue import reconcile
from loom.service.autonomy import AutonomyService
from loom.service.contracts import canonical
from tests.autonomy.test_dispatch import prepare
from tests.autonomy.test_learning import terminal
from tests.autonomy.test_learning_budget import deny, setup


def background(jobs, starts):
    def start(sid, analysis, options, **kwargs):
        starts.append(options)
        job = jobs[options["resume_id"]]
        job.update(state="queued", host_resume_requests=[*job.get("host_resume_requests", []), kwargs["resume_request_id"]])

    return SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(_load=lambda sid, analysis, eid: jobs[eid], _public=lambda j: {"retryable_work": False}, start=start),
    )


def second(store, cycle, **overrides):
    other = dict(cycle, id="second", run_id="second-run", evaluation_id="second-eval", state="pending", queue_order=2, **overrides)
    with store.sessions.transaction() as db:
        cycle["queue_order"] = 1
        store.put(db, "agent_learning_cycles", cycle)
        store.put(db, "agent_learning_cycles", other)
    return other


def test_running_head_excludes_later_jobs_even_when_unchanged(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    other = second(store, cycle)
    job["state"] = "running"
    jobs = {job["id"]: job, other["evaluation_id"]: {"state": "completed"}}
    coordinator = LearningCoordinator(store, background(jobs, []))
    for _ in range(3):
        coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][1]["state"] == "pending"
    job["state"] = "completed"
    coordinator.advance(cycle["agent_id"])
    coordinator.advance(cycle["agent_id"])
    assert all(c["state"] == "completed" for c in store.snapshot(cycle["agent_id"])["learning"])


def test_budget_wait_is_bounded_and_retains_memory_before_next_job(tmp_path):
    store, clock, ledger, cycle, job = setup(tmp_path, policy={"auto_expand": False, "max_attempts": 2})
    other = second(store, cycle)
    job["checkpoint"]["memory"]["records"] = {"accepted": {"kind": "facts", "publication_state": "published"}}
    original = deepcopy(job["checkpoint"])
    deny(store, ledger, job)
    starts = []
    coordinator = LearningCoordinator(store, background({job["id"]: job, other["evaluation_id"]: {"state": "completed"}}, starts))
    coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "deferred"
    clock.advance(61)
    coordinator.advance(cycle["agent_id"])
    result = store.snapshot(cycle["agent_id"])["learning"][0]
    assert result["state"] == "incomplete" and result["budget_waits"] == 2
    assert result["memory_result"]["accepted"] == 1 and result["coverage"] == "partial"
    assert not result["auto_resume"] and not result["retry_after"]
    coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][1]["state"] == "completed"
    assert job["checkpoint"] == original and not starts


def test_manual_retry_requeues_at_tail_and_starts_new_round_without_resetting_usage(tmp_path):
    store, _, ledger, cycle, job = setup(tmp_path, policy={"max_attempts": 2})
    cycle.update(state="incomplete", attempts=2, completion_policy_seen=1)
    other = second(store, cycle)
    with store.sessions.transaction() as db:
        db.execute("CREATE TABLE semantic_jobs(id TEXT PRIMARY KEY,body TEXT)")
    facade = SimpleNamespace(store=store)
    command = dict(command_id="manual-retry", type="retry")
    result = AutonomyService.learning_command(facade, cycle["agent_id"], cycle["id"], command)
    assert AutonomyService.learning_command(facade, cycle["agent_id"], cycle["id"], command) == result
    assert result["queue_order"] > other["queue_order"] and result["round_start_attempts"] == 2
    assert result["attempts"] == 2 and result["retry_round"] == 1
    before_usage = ledger.summary(cycle["agent_id"])
    starts = []
    coordinator = LearningCoordinator(store, background({job["id"]: job, other["evaluation_id"]: {"state": "completed"}}, starts))
    coordinator.advance(cycle["agent_id"])
    assert not starts
    coordinator.advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert saved["state"] == "running" and saved["attempts"] == 3
    assert starts == [{"resume_id": cycle["evaluation_id"]}]
    assert ledger.summary(cycle["agent_id"]) == before_usage


def test_backfill_is_ordered_pinned_idempotent_and_does_not_rewrite_work(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sid = facade.snapshot(aid)["sessions"][0]["session_id"]
    terminal(service, sid, rid="first")
    terminal(service, sid, rid="second", status="failed")
    service.store.update(sid, lambda state, emit, db: emit("later.input", {"not_evidence": True}))
    terminal(service, sid, rid="active", status="suspended")
    before = facade.snapshot(aid)["work_packages"]
    with service.store.transaction() as db:
        db.execute("DELETE FROM agent_learning_cycles WHERE agent_id=?", (aid,))
        cycles = reconcile(facade.store, db, aid)
        assert [c["run_id"] for c in cycles] == ["first", "second"]
        assert cycles[0]["queue_order"] < cycles[1]["queue_order"]
        for cycle in cycles:
            event = db.execute("SELECT body FROM events WHERE session_id=? AND seq=?", (sid, cycle["terminal_event_cursor"])).fetchone()[0]
            assert "run.state.changed" in event
        assert reconcile(facade.store, db, aid) == cycles
    assert facade.snapshot(aid)["work_packages"] == before


def test_failure_event_cannot_enqueue_before_atomic_terminal_commit(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sid = facade.snapshot(aid)["sessions"][0]["session_id"]
    terminal(service, sid, rid="failing", status="running")
    service.store.update(sid, lambda state, emit, db: emit("run.failed", {"message": "worker failure before host cleanup"}))
    with service.store.transaction() as db:
        assert reconcile(facade.store, db, aid) == []
    assert facade.snapshot(aid)["work_packages"][0]["state"] == "running"

    def finish(state, emit, db):
        state["run"]["state"] = "failed"
        state["task"]["state"] = "failed"

    service.store.update(sid, finish)
    result = facade.snapshot(aid)
    assert result["work_packages"][0]["state"] == "failed"
    assert len(result["learning"]) == 1


def test_timer_respects_interval_and_manual_check_runs_same_queue(tmp_path):
    store, clock, _, cycle, job = setup(tmp_path)
    job["state"] = "running"
    coordinator = LearningCoordinator(store, background({job["id"]: job}, []))
    coordinator.advance(cycle["agent_id"], manual=False)
    job["state"] = "completed"
    coordinator.advance(cycle["agent_id"], manual=False)
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "running"
    coordinator.advance(cycle["agent_id"], manual=True)
    assert store.snapshot(cycle["agent_id"])["learning"][0]["state"] == "completed"


def test_cancelled_call_must_drain_before_next_admission(tmp_path):
    store, _, _, cycle, job = setup(tmp_path)
    cycle["state"] = "cancelled"
    other = second(store, cycle)
    with store.sessions.transaction() as db:
        db.execute("CREATE TABLE semantic_jobs(id TEXT PRIMARY KEY,body TEXT)")
        db.execute("INSERT INTO semantic_jobs VALUES(?,?)", (job["id"], canonical(dict(job, state="running"))))
    coordinator = LearningCoordinator(store, background({job["id"]: job, other["evaluation_id"]: {"state": "completed"}}, []))
    coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][1]["state"] == "pending"
    with store.sessions.transaction() as db:
        db.execute("UPDATE semantic_jobs SET body=?", (canonical(dict(job, state="cancelled")),))
    coordinator.advance(cycle["agent_id"])
    assert store.snapshot(cycle["agent_id"])["learning"][1]["state"] == "completed"


def test_manual_queue_check_is_idempotent_and_independent_of_execution_planner(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent.update(scheduler={"state": "planning"}, next_learning_at="2099-01-01")
        facade.store.put(db, "agents", agent)
    body = {"command_id": "manual-learning-check"}
    result = facade.dispatch("POST", [aid, "learning", "schedule"], {}, body)
    assert facade.dispatch("POST", [aid, "learning", "schedule"], {}, body) == result
    assert facade.snapshot(aid)["learning_schedule_requested"]
    facade.supervisor.learning.advance(aid, manual=False)
    state = facade.snapshot(aid)
    assert not state["learning_schedule_requested"] and state["scheduler"]["state"] == "planning"


def test_lost_initial_start_reply_adopts_same_job_after_restart(tmp_path):
    store, clock, _, cycle, job = setup(tmp_path)
    cycle.pop("evaluation_id")
    cycle.update(state="pending", attempts=0)
    with store.sessions.transaction() as db:
        store.put(db, "agent_learning_cycles", cycle)
    listing = {"run_ids": [cycle["run_id"]], "jobs": []}
    calls = []

    def start(sid, analysis, options, **kwargs):
        calls.append(options)
        job.update(run_id=cycle["run_id"], state="queued", settings={"extract_memory": True})
        listing["jobs"].append(job)
        raise OSError("reply lost after admission")

    adapter = SimpleNamespace(
        trajectory=SimpleNamespace(get=lambda *args: {"state": "completed"}),
        semantic=SimpleNamespace(list=lambda *args: listing, start=start, _load=lambda *args: job),
    )
    LearningCoordinator(store, adapter).advance(cycle["agent_id"])
    clock.advance(61)
    LearningCoordinator(store, adapter).advance(cycle["agent_id"])
    saved = store.snapshot(cycle["agent_id"])["learning"][0]
    assert len(calls) == 1 and saved["evaluation_id"] == job["id"]
    assert saved["attempts"] == 1 and saved["state"] == "running"
