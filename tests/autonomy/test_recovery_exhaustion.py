import json

from tests.autonomy.helpers import goals
from tests.autonomy.test_queue_scheduling import paused_work


def events(facade, aid):
    with facade.store.sessions.transaction() as db:
        return [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_events WHERE agent_id=? ORDER BY seq", (aid,))]


def test_unrecoverable_reason_counts_each_attempt(agent_service):
    service, facade, aid, work, sid = paused_work(agent_service, "某些无法识别的原因")
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert snap["scheduler"]["state"] == "intervention"
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["work_packages"][0]["recovery"]["attempts"] == 2
    assert snap["scheduler"]["state"] == "intervention"


def test_premise_failure_does_not_count(agent_service):
    control = {"kind": "paused", "command_id": "human-1", "reason": "pause"}
    service, facade, aid, work, sid = paused_work(agent_service, control=control)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "intervention"
    assert not snap["work_packages"][0].get("recovery")


def test_exhaustion_suspends_and_stops_retrying(agent_service):
    service, facade, aid, work, sid = paused_work(agent_service, "某些无法识别的原因")
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["scheduling_policy"]["max_recovery_attempts"] = 2
        facade.store.put(db, "agents", agent)
    facade.supervisor.scheduler.advance(aid, manual=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)["work_packages"][0]["recovery"]["attempts"] == 2

    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["work_packages"][0]["blocked_reason"] == "recovery_exhausted"
    exhausted = [e for e in events(facade, aid) if e["type"] == "schedule.recovery.exhausted"]
    assert len(exhausted) == 1 and exhausted[0]["payload"]["attempts"] == 2

    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["work_packages"][0]["recovery"]["attempts"] == 2
    assert [e for e in events(facade, aid) if e["type"] == "schedule.recovery.exhausted"] == exhausted


def test_exhausted_work_does_not_block_other_dispatch(agent_service):
    service, facade, aid, work, sid = paused_work(agent_service, "某些无法识别的原因")
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["scheduling_policy"]["max_recovery_attempts"] = 0
        facade.store.put(db, "agents", agent)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)["work_packages"][0]["blocked_reason"] == "recovery_exhausted"

    # The planner will not materialize new work while another package is
    # blocked, so the next goal's package is inserted directly.
    goal2 = facade.store.add_goal(aid, "goal-2", goals()[1])
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        facade.store.put(db, "agent_work_packages", dict(
            id="wp-2",
            agent_id=aid,
            goal_id=goal2["id"],
            goal_revision=goal2["revision"],
            agent_run_id=None,
            role_revision=agent["role_revision"],
            state="pending",
            order=0,
            created_at=facade.store.now(),
            run_ids=[],
            read_only=False,
            stage_id=None,
            roadmap_id=None,
            objective_id=goal2["long_term_objective_id"],
            objective=goal2["objective"],
            acceptance=goal2["acceptance"],
            reason="Explicit user goal",
        ))
    facade.supervisor.scheduler.advance(aid, manual=True)

    snap = facade.snapshot(aid)
    assert len(snap["work_packages"]) == 2
    second = next(w for w in snap["work_packages"] if w["goal_id"] == goal2["id"])
    assert second["state"] == "dispatched"
    assert second["session_id"] != sid
    assert len(service.store.list_sessions()) == 2
