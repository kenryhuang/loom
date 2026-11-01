import json

from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals, role


def prepare(facade, aid):
    facade.store.add_goal(aid, "goal", goals()[0])
    facade.supervisor._plan(facade.snapshot(aid))
    return facade.snapshot(aid)["work_packages"][0]


def recommend(**overrides):
    return {
        "task_type": "general",
        "tool_collections": ["filesystem", "shell", "task_control"],
        "plugins": {"context": "bounded_context", "workflow": "legacy_planning"},
        "plan_mode": "force",
        "rationale": "需要命令执行与分阶段计划",
        "collection_reasons": {},
        **overrides,
    }


def agent_events(facade, aid):
    with facade.store.sessions.transaction() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM agent_events WHERE agent_id=? ORDER BY seq", (aid,))]


def test_dispatch_applies_recommended_binding(agent_service):
    service, facade, aid, _ = agent_service
    service.setup_recommender = lambda objective, *, model, scope, extra=None: recommend()
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sessions = service.store.list_sessions()
    assert len(sessions) == 1
    task = sessions[0]["task"]
    assert set(task["task_spec"]["tools"]["collections"]) == {"filesystem", "shell", "task_control"}
    assert task["task_spec"]["workflow"]["plugin"] == "legacy_planning"
    assert task["plan_mode"] == "force"
    work = facade.snapshot(aid)["work_packages"][0]
    assert work["binding"]["source"] == "recommended"
    assert work["binding"]["rationale"] == "需要命令执行与分阶段计划"
    assert any(event["type"] == "schedule.binding.recommended" for event in agent_events(facade, aid))


def test_dispatch_falls_back_to_role_on_recommend_failure(agent_service):
    service, facade, aid, path = agent_service
    agent = facade.store.create(
        "create-2",
        {
            **role(path),
            "task_spec": {
                "session_environment": {
                    "plugin": "session",
                    "resources": [{"id": "workspace", "kind": "directory", "uri": str(path), "access": "read-write"}],
                },
                "tools": {"collections": ["filesystem", "task_control"]},
            },
            "plan_mode": "force",
        },
    )
    facade.store.command(agent["id"], "enable-2", "enable", 1)
    aid = agent["id"]

    def fail(objective, *, model, scope, extra=None):
        raise ServiceError("recommendation unavailable", 502)

    service.setup_recommender = fail
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sessions = service.store.list_sessions()
    assert len(sessions) == 1
    task = sessions[0]["task"]
    assert task["task_spec"]["tools"]["collections"] == ["filesystem", "task_control"]
    assert task["plan_mode"] == "force"
    work = facade.snapshot(aid)["work_packages"][0]
    assert work["binding"]["source"] == "fallback"
    assert "recommendation unavailable" in work["binding"]["reason"]
    assert any(event["type"] == "schedule.binding.fallback" for event in agent_events(facade, aid))


def test_read_only_work_skips_recommend(agent_service):
    service, facade, aid, _ = agent_service

    def forbidden(objective, *, model, scope, extra=None):
        raise AssertionError("read_only work must not call the recommender")

    service.setup_recommender = forbidden
    work = prepare(facade, aid)
    with facade.store.sessions.transaction() as db:
        row = facade.store.get(db, "agent_work_packages", work["id"], aid)
        row["read_only"] = True
        facade.store.put(db, "agent_work_packages", row)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    sessions = service.store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["task"]["task_spec"]["tools"]["collections"] == ["filesystem", "task_control"]
    assert "binding" not in facade.snapshot(aid)["work_packages"][0]


def idle_bound_session(service, facade, aid, path):
    sid = service.create("manual", {"workspace": str(path)})["session_id"]
    facade.session_command(aid, sid, dict(command_id="bind", type="bind", expected_revision=2))
    with service.store.transaction() as db:
        state = service.store._load(db, sid)
        state["task"]["objective"] = "核对 Session 状态"
        service.store._save(db, state)
    return sid


def test_reuse_rejects_candidate_with_mismatched_collections(agent_service):
    service, facade, aid, path = agent_service
    sid = idle_bound_session(service, facade, aid, path)
    service.setup_recommender = lambda objective, *, model, scope, extra=None: recommend(
        tool_collections=["browser", "filesystem", "shell", "task_control"], plan_mode="auto"
    )
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    bound = facade.snapshot(aid)["work_packages"][0]
    assert bound["session_id"] != sid
    with facade.store.sessions.transaction() as db:
        route = facade.store.rows(db, "agent_routes", aid)[0]
    assert route["action"] == "create"
    assert route["candidates"][0]["reason"] == "incompatible_configuration"
    assert len(service.store.list_sessions()) == 2


def test_reuse_kept_when_binding_matches_candidate(agent_service):
    service, facade, aid, path = agent_service
    sid = idle_bound_session(service, facade, aid, path)
    # Manual sessions normalize to filesystem/shell/task_control + bounded_context + legacy_planning + auto.
    service.setup_recommender = lambda objective, *, model, scope, extra=None: recommend(plan_mode="auto")
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    bound = facade.snapshot(aid)["work_packages"][0]
    assert bound["session_id"] == sid
    with facade.store.sessions.transaction() as db:
        route = facade.store.rows(db, "agent_routes", aid)[0]
    assert route["action"] == "reuse"
    assert len(service.store.list_sessions()) == 1
