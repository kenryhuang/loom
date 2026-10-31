import json

from tests.autonomy.test_dispatch import prepare


def terminal(service, sid, rid="r1", status="completed"):
    def apply(state, emit, db):
        state["run"] = dict(id=rid, state=status, attempt_id="attempt", steps=1)
        emit("run.started", {"task_contract": {"objective": state["task"]["objective"]}})
        emit("llm.requested", {"llm_call_id": "call", "messages": [{"role": "user", "content": state["task"]["objective"]}], "tools": []})
        emit("llm.completed", {"llm_call_id": "call", "response": {"content": "Finished review"}})
        if status == "completed":
            emit("acceptance.gate.passed", {"results": []})
        emit("run.state.changed", {"state": status})
        state["task"]["state"] = "idle"

    service.store.update(sid, apply)


def test_terminal_commit_is_atomic_idempotent_and_snapshot_is_pinned(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sid = facade.snapshot(aid)["sessions"][0]["session_id"]
    terminal(service, sid)
    cycle = facade.snapshot(aid)["learning"][0]
    cursor = cycle["terminal_event_cursor"]
    service.store.update(sid, lambda state, emit, db: emit("later.input", {"secret": "only-later-run"}))
    assert len(facade.snapshot(aid)["learning"]) == 1
    assert facade.snapshot(aid)["learning"][0]["terminal_event_cursor"] == cursor
    trajectory = service.background.trajectory
    from tests.autonomy.test_execution_flow import until

    analysis = trajectory.start(sid, source_cursor=cursor)
    until(lambda: trajectory.get(sid, analysis["id"]), lambda a: a["state"] == "completed")
    source, _ = trajectory._data(trajectory._load(sid, analysis["id"]))
    assert "only-later-run" not in str(source.events)
    assert trajectory.start(sid, source_cursor=cursor)["id"] == analysis["id"]


def test_suspended_does_not_create_learning_and_failed_does(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sid = facade.snapshot(aid)["sessions"][0]["session_id"]
    terminal(service, sid, status="suspended")
    assert facade.snapshot(aid)["learning"] == []

    def failed(state, emit, db):
        state["run"]["state"] = "failed"
        emit("run.failed", {"message": "explicit failed execution"})

    service.store.update(sid, failed)
    assert len(facade.snapshot(aid)["learning"]) == 1
    assert facade.snapshot(aid)["work_packages"][0]["state"] == "failed"


def test_no_model_calls_on_empty_periods_and_disabled_service(agent_service):
    _, facade, aid, _ = agent_service

    def forbidden(*args):
        raise AssertionError("No ready goals")

    facade.supervisor.planner.provider = forbidden
    facade.supervisor.tick()
    facade.supervisor.tick()
    assert facade.supervisor.thread is None
    with facade.store.sessions.transaction() as db:
        assert len(facade.store.rows(db, "agent_runs", aid)) == 1
        assert json.loads(db.execute("SELECT body FROM agent_runs").fetchone()[0])["state"] == "no_action"
