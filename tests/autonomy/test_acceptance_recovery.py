import json

from tests.autonomy.test_queue_scheduling import paused_work


def events(facade, aid):
    with facade.store.sessions.transaction() as db:
        return [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_events WHERE agent_id=? ORDER BY seq", (aid,))]


def acceptance_blocked_work(agent_service):
    reason = "Acceptance blocked: UI 视觉验证缺 browser capture"
    service, facade, aid, work, sid = paused_work(agent_service, reason)

    def blocked(state, emit, db):
        state["acceptance"] = {
            "state": "blocked",
            "reason": reason,
            "plan": {"unresolved_requirements": ["UI 视觉验证缺 browser capture"]},
        }

    service.store.update(sid, blocked)
    return service, facade, aid, work, sid


def test_acceptance_blocked_work_restarts_with_repaired_spec(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    seen = {}

    def recommender(text, *, model, scope, extra=None):
        seen.update(scope=scope, extra=extra)
        return dict(
            task_type="general",
            tool_collections=["filesystem", "shell", "browser", "task_control"],
            rationale="补全 browser 以完成视觉验收",
            collection_reasons={"browser": "视觉验证"},
        )

    service.setup_recommender = recommender
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "recovered", snap["scheduler"]
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert seen["scope"] == "repair"
    assert seen["extra"]["unresolved_requirements"] == ["UI 视觉验证缺 browser capture"]
    state = service.snapshot(sid)
    assert state["task"]["state"] == "queued"
    assert "browser" in state["task"]["task_spec"]["tools"]["collections"]
    with service.store.transaction() as db:
        row = db.execute("SELECT input FROM commands WHERE id LIKE 'schedule_resume%'").fetchone()
    command = json.loads(row[0])
    assert command["type"] == "restart_task"
    assert "browser" in command["payload"]["task"]["task_spec"]["tools"]["collections"]
    requested = [e for e in events(facade, aid) if e["type"] == "schedule.recovery.requested"]
    assert requested and requested[0]["payload"]["kind"] == "restart"


def test_repair_without_additions_counts_and_reports_unrecoverable(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    seen = {}

    def recommender(text, *, model, scope, extra=None):
        seen.update(scope=scope)
        return dict(
            task_type="general",
            tool_collections=["filesystem", "shell", "task_control"],
            rationale="现有工具已足够",
            collection_reasons={},
        )

    service.setup_recommender = recommender
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert seen.get("scope") == "repair"
    assert snap["scheduler"]["state"] == "intervention"
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert service.snapshot(sid)["task"]["state"] == "paused"
    with service.store.transaction() as db:
        assert db.execute("SELECT 1 FROM commands WHERE id LIKE 'schedule_resume%'").fetchone() is None


def test_read_only_work_is_not_repaired(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    called = []

    def recommender(text, *, model, scope, extra=None):
        called.append(scope)
        raise AssertionError("read_only 工作包不应触发修复推荐")

    service.setup_recommender = recommender
    with service.store.transaction() as db:
        current = facade.store.get(db, "agent_work_packages", work["id"], aid)
        current["read_only"] = True
        facade.store.put(db, "agent_work_packages", current)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "intervention"
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert not called
