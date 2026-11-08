import json

import pytest

from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals, role


def prepare(facade, aid):
    facade.store.add_goal(aid, "goal", goals()[0])
    facade.supervisor._plan(facade.snapshot(aid))
    return facade.snapshot(aid)["work_packages"][0]


def test_create_and_dispatch_with_workspace_and_form_tool_spec(agent_service):
    service, facade, _, path = agent_service
    directory = path / "project"
    directory.mkdir()
    spec = {"schema_version": 1, "tools": {"collections": ["filesystem", "shell", "task_control"]}}
    agent = facade.dispatch("POST", [], {}, {
        "command_id": "create-form-agent", "role": {**role(path), "working_directory": str(directory), "task_spec": spec},
    })
    assert agent["role"]["task_spec"] == spec
    facade.store.command(agent["id"], "enable-form-agent", "enable", 1)
    work = prepare(facade, agent["id"])
    facade.supervisor.dispatcher.dispatch(agent["id"], work["id"])
    task = service.store.list_sessions()[0]["task"]
    assert task["workspace"] == str(directory)
    assert task["task_spec"]["session_environment"]["resources"] == [
        {"id": "workspace", "kind": "directory", "uri": str(directory), "access": "read-write"},
    ]
    assert task["task_spec"]["tools"]["collections"] == spec["tools"]["collections"]


def test_create_preserves_explicit_read_only_workspace(agent_service):
    _, facade, _, path = agent_service
    from loom.autonomy.dispatch import task_payload
    from loom.service.contracts import validate_create

    resource = {"id": "source", "kind": "directory", "uri": str(path), "access": "read-only"}
    spec = {"session_environment": {"plugin": "session", "resources": [resource]},
            "tools": {"collections": ["filesystem", "task_control"]}}
    agent = facade.store.create("create-read-only", {**role(path), "task_spec": spec})
    task = validate_create(task_payload(agent, dict(objective="Inspect files", acceptance=[])))
    assert task["task_spec"]["session_environment"]["resources"] == [resource]
    assert task["task_spec"]["execution_runtime"]["workspace_resource"] == "source"


def test_create_file_tools_still_requires_a_workspace(agent_service):
    _, facade, _, path = agent_service
    with pytest.raises(ServiceError, match="explicit workspace resource"):
        facade.store.create("create-unbound", {**role(path), "workspace": None, "source_routes": [],
            "task_spec": {"tools": {"collections": ["filesystem", "shell", "task_control"]}}})


@pytest.mark.parametrize("fallback", [False, True])
def test_dispatch_closes_minimal_tools_over_contract_even_on_recommendation_fallback(agent_service, fallback):
    service, facade, aid, path = agent_service
    seen = []
    def recommend_minimal(text, *, model, scope, extra=None):
        seen.append(extra)
        if fallback:
            raise ServiceError("offline")
        return recommend(tool_collections=["filesystem", "task_control"])
    service.setup_recommender = recommend_minimal
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        agent["role"]["task_spec"] = {"session_environment": {"plugin": "session", "resources": [
            {"id": "workspace", "kind": "directory", "uri": str(path), "access": "read-write"}]},
            "tools": {"collections": ["filesystem", "task_control"]}}
        facade.store.put(db, "agents", agent)
    work = prepare(facade, aid)
    with service.store.transaction() as db:
        work = facade.store.get(db, "agent_work_packages", work["id"], aid)
        work["required_tools"] = ["read_file", "shell_execute"]
        facade.store.put(db, "agent_work_packages", work)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    task = service.store.list_sessions()[0]["task"]
    assert set(task["task_spec"]["tools"]["collections"]) == {"filesystem", "task_control", "shell"}
    assert seen == [{"required_tools": ["read_file", "shell_execute"]}]
    assert facade.snapshot(aid)["work_packages"][0]["binding"]["added_collections"] == ["shell"]


@pytest.mark.parametrize("required,readonly", [(["missing_plugin_tool"], False), (["shell_execute"], True)])
def test_unsatisfied_contract_is_rejected_before_creating_worker_session(agent_service, required, readonly):
    service, facade, aid, _ = agent_service
    service.setup_recommender = lambda *args, **kwargs: recommend(tool_collections=["filesystem", "task_control"])
    work = prepare(facade, aid)
    with service.store.transaction() as db:
        work = facade.store.get(db, "agent_work_packages", work["id"], aid)
        work.update(required_tools=required, read_only=readonly)
        facade.store.put(db, "agent_work_packages", work)
    with pytest.raises(ServiceError, match=required[0]):
        facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert not service.store.list_sessions()


def test_registered_external_requirement_uses_existing_binding_without_extra_collections(agent_service):
    from loom.autonomy.binding import ensure_required_tools
    from tests.external_tools.test_external_tools import cli_config

    service, _, _, path = agent_service
    tid = service.store.external_tools.save(cli_config())["tools"][0]["id"]
    payload, additions = ensure_required_tools(service, dict(objective="Use echo", workspace=str(path),
        external_tool_ids=[tid], task_spec={"tools": {"collections": ["task_control"]}}), [tid])
    assert payload["task_spec"]["tools"]["collections"] == ["task_control", "external_tools"]
    assert additions == []


def test_dispatch_accepts_scoped_runtime_memory_requirement(agent_service):
    service, facade, aid, _ = agent_service
    service.setup_recommender = lambda *args, **kwargs: recommend(tool_collections=["filesystem", "task_control"])
    work = prepare(facade, aid)
    with service.store.transaction() as db:
        work = facade.store.get(db, "agent_work_packages", work["id"], aid)
        work["required_tools"] = ["read_file", "memory_read"]
        facade.store.put(db, "agent_work_packages", work)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert facade.snapshot(aid)["work_packages"][0]["session_id"]


def test_dispatch_rejects_runtime_memory_without_scope(agent_service):
    from loom.autonomy.binding import ensure_required_tools

    service, _, _, _ = agent_service
    with pytest.raises(ServiceError, match="memory_read"):
        ensure_required_tools(service, dict(objective="Read memory", task_spec={"tools": {"collections": ["task_control"]}}), ["memory_read"])


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


def idle_bound_session(service, facade, aid, path, **create):
    sid = service.create("manual", {"workspace": str(path), **create})["session_id"]
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


def test_reuse_ignores_role_derived_collections(agent_service):
    service, facade, aid, path = agent_service
    base = service.store.knowledge.create({"name": "AI Infra", "description": "AI systems", "engine": "sqlite_fts"})
    agent = facade.store.create("create-kb", {**role(path), "knowledge_base_ids": [base["id"]]})
    facade.store.command(agent["id"], "enable-kb", "enable", 1)
    aid = agent["id"]
    sid = idle_bound_session(service, facade, aid, path, knowledge_base_ids=[base["id"]])
    # validate_create materializes the role-level binding into the session spec.
    assert "knowledge" in service.snapshot(sid)["task"]["task_spec"]["tools"]["collections"]
    # The dispatch prompt omits role-level bindings, so the recommendation never
    # carries the knowledge collection; compatibility must ignore the derived diff.
    service.setup_recommender = lambda objective, *, model, scope, extra=None: recommend(plan_mode="auto")
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    bound = facade.snapshot(aid)["work_packages"][0]
    assert bound["session_id"] == sid
    with facade.store.sessions.transaction() as db:
        route = facade.store.rows(db, "agent_routes", aid)[0]
    assert route["action"] == "reuse"
    assert len(service.store.list_sessions()) == 1


def test_undispatchable_work_never_calls_the_recommender(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    with facade.store.sessions.transaction() as db:
        goal = facade.store.get(db, "agent_goals", work["goal_id"], aid)
        goal["state"] = "blocked"
        facade.store.put(db, "agent_goals", goal)
    called = []

    def spy(objective, *, model, scope, extra=None):
        called.append(scope)
        return recommend()

    service.setup_recommender = spy
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert not called
    assert not service.store.list_sessions()
    work = facade.snapshot(aid)["work_packages"][0]
    assert work["state"] == "pending" and "binding" not in work
