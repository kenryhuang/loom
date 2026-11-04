import json

from loom.core import ok
from loom.llm.api import LlmResponse, TokenUsage
from tests.autonomy.helpers import role
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


def test_read_only_work_enters_repair_and_recommender_failures_are_bounded(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    called = []

    def recommender(text, *, model, scope, extra=None):
        called.append(scope)
        raise RuntimeError("推荐服务不可用")

    service.setup_recommender = recommender
    with service.store.transaction() as db:
        current = facade.store.get(db, "agent_work_packages", work["id"], aid)
        current["read_only"] = True
        facade.store.put(db, "agent_work_packages", current)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "intervention"
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert called == ["repair"]


class RepairProvider:
    """Drives the real recommend_setup validation path through the service seam."""

    model = "repair-test"

    def __init__(self, value, during=None):
        self.value, self.during = value, during

    async def chat(self, messages, tools=None, cancellation=None):
        assert tools is None
        if self.during:
            self.during()
        return ok(LlmResponse(json.dumps(self.value), usage=TokenUsage(20, 10, 30)))


def repair_proposal(collections, **changes):
    return {
        "task_type": "general",
        "tool_collections": collections,
        "external_tools": [],
        "rationale": "补全验收所需工具",
        "collection_reasons": {},
        **changes,
    }


def test_repair_tolerates_role_derived_collections_in_current(agent_service):
    service, facade, aid, path = agent_service
    base = service.store.knowledge.create({"name": "AI Infra", "description": "AI systems", "engine": "sqlite_fts"})
    agent = facade.store.create("create-kb", {**role(path), "knowledge_base_ids": [base["id"]]})
    facade.store.command(agent["id"], "enable-kb", "enable", 1)
    service, facade, aid, work, sid = acceptance_blocked_work((service, facade, agent["id"], path))
    current = service.snapshot(sid)["task"]["task_spec"]["tools"]["collections"]
    assert "knowledge" in current  # validate_create materializes the role-level binding

    # The repair prompt tells the model not to re-select role-level bindings, so
    # the proposal omits knowledge; validation must tolerate that omission.
    service.setup_provider_factory = lambda _: RepairProvider(
        repair_proposal(["filesystem", "shell", "browser", "task_control"])
    )
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "recovered", snap["scheduler"]
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    state = service.snapshot(sid)
    assert state["task"]["state"] == "queued"
    collections = state["task"]["task_spec"]["tools"]["collections"]
    assert "browser" in collections and "knowledge" in collections
    with service.store.transaction() as db:
        row = db.execute("SELECT input FROM commands WHERE id LIKE 'schedule_resume%'").fetchone()
    command = json.loads(row[0])
    assert command["type"] == "restart_task"
    restarted = command["payload"]["task"]["task_spec"]["tools"]["collections"]
    assert "browser" in restarted and "knowledge" in restarted


def test_repair_validation_rejection_counts_as_decision_failure(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    current = service.snapshot(sid)["task"]["task_spec"]["tools"]["collections"]
    assert "shell" in current

    # The proposal drops shell — a repair may never remove a collection.
    service.setup_provider_factory = lambda _: RepairProvider(repair_proposal(["filesystem", "task_control"]))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "intervention"
    assert "Repair may not remove" in snap["scheduler"]["reason"]
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    assert service.snapshot(sid)["task"]["state"] == "paused"
    with service.store.transaction() as db:
        assert db.execute("SELECT 1 FROM commands WHERE id LIKE 'schedule_resume%'").fetchone() is None


def test_acceptance_repair_does_not_require_a_checkpoint(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)

    def clear(state, emit, db):
        state["run"]["checkpoint"] = None

    service.store.update(sid, clear)
    assert not service.snapshot(sid).get("context")
    service.setup_provider_factory = lambda _: RepairProvider(
        repair_proposal(["filesystem", "shell", "browser", "task_control"])
    )
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "recovered", snap["scheduler"]
    assert snap["work_packages"][0]["recovery"]["attempts"] == 1
    with service.store.transaction() as db:
        row = db.execute("SELECT input FROM commands WHERE id LIKE 'schedule_resume%'").fetchone()
    assert json.loads(row[0])["type"] == "restart_task"


def test_human_control_during_repair_recommendation_aborts_without_counting(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)

    def seize():
        service.store.update(sid, lambda state, emit, db: state.update(control={"command_id": "human-pause", "type": "pause"}))

    service.setup_provider_factory = lambda _: RepairProvider(
        repair_proposal(["filesystem", "shell", "browser", "task_control"]), during=seize
    )
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap["scheduler"]["state"] == "intervention"
    assert (snap["work_packages"][0].get("recovery") or {}).get("attempts", 0) == 0
    assert service.snapshot(sid)["task"]["state"] == "paused"
    # service.snapshot is a view model; the control lease lives on the raw state.
    assert (service.store.snapshot(sid).get("control") or {}).get("command_id") == "human-pause"
    with service.store.transaction() as db:
        assert db.execute("SELECT 1 FROM commands WHERE id LIKE 'schedule_resume%'").fetchone() is None
