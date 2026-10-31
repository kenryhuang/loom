import pytest

from loom.autonomy.context import build_snapshot
from loom.autonomy.profile import command, feedback, review_due
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import Clock


def explicit(aid, facade, key="language", content="Use Chinese", scope="agent"):
    return feedback(facade.store, aid, dict(command_id="profile-" + key + content + scope, key=key, text=content, scope=scope, kind="explicit"))


def test_explicit_once_lock_conflict_retire_and_expiry(agent_service):
    _, facade, aid, _ = agent_service
    facade.store.clock = Clock()
    item = explicit(aid, facade)
    assert build_snapshot(facade.store, aid)["preferences"][0]["text"] == "Use Chinese"
    locked = command(facade.store, aid, dict(command_id="lock", entry_id=item["id"], expected_revision=item["revision"], type="lock"))
    conflict = explicit(aid, facade, content="Use English")
    assert conflict["text"] == "Use Chinese" and conflict["counterexamples"] and conflict["locked_by_user"]
    retired = command(facade.store, aid, dict(command_id="retire", entry_id=item["id"], expected_revision=conflict["revision"], type="retire"))
    assert retired["state"] == "retired" and not build_snapshot(facade.store, aid)["preferences"]
    assert locked["revision"] == 2
    explicit(aid, facade, key="style")
    facade.store.clock.advance(2592001)
    review_due(facade.store, aid)
    assert not build_snapshot(facade.store, aid)["preferences"]


def test_feedback_rejects_agent_dispatched_user_role_and_manufactured_episodes(agent_service):
    service, facade, aid, workspace = agent_service
    sid = service.store.create("session", dict(objective="agent request", workspace=str(workspace)), producer="agent_planner")["session_id"]
    with service.store.transaction() as db:
        state = service.store._load(db, sid)
        db.execute("INSERT INTO agent_sessions VALUES(?,?,NULL,0)", (sid, aid))
    with pytest.raises(ServiceError, match="human message"):
        feedback(
            facade.store,
            aid,
            dict(
                command_id="feedback", key="style", text="concise", session_id=sid, message_id=state["messages"][0]["id"], kind="adopt", episode_id="invented"
            ),
        )
    with pytest.raises(ServiceError, match="independent"):
        feedback(facade.store, aid, dict(command_id="untrusted", key="style", text="concise", kind="adopt", episode_id="invented"))


def test_independent_feedback_trial_conflict_and_rejection_cannot_restore(agent_service):
    service, facade, aid, workspace = agent_service
    item = None
    for i in range(2):
        sid = service.store.create(f"human-{i}", dict(objective="Please use concise output", workspace=str(workspace)))["session_id"]
        with service.store.transaction() as db:
            state = service.store._load(db, sid)
            db.execute("INSERT INTO agent_sessions VALUES(?,?,NULL,0)", (sid, aid))
        body = dict(command_id=f"adopt-{i}", key="style", text="Use concise output", session_id=sid, message_id=state["messages"][0]["id"], kind="adopt")
        item = feedback(facade.store, aid, body)
        repeated = feedback(facade.store, aid, {**body, "command_id": f"repeat-{i}"})
        assert repeated["independent_episodes"] == item["independent_episodes"]
        item = repeated
    assert item["state"] == "candidate" and item["independent_episodes"] == 2
    trial = command(facade.store, aid, dict(command_id="trial", entry_id=item["id"], expected_revision=item["revision"], type="trial"))
    assert trial["trial"] and build_snapshot(facade.store, aid)["preferences"]
    rejected = feedback(facade.store, aid, {**body, "command_id": "reject", "kind": "reject"})
    assert rejected["state"] == "disputed" and not build_snapshot(facade.store, aid)["preferences"]
    retired = command(facade.store, aid, dict(command_id="retired", entry_id=item["id"], expected_revision=rejected["revision"], type="retire"))
    replayed = feedback(facade.store, aid, {**body, "command_id": "scan-again"})
    assert retired["state"] == replayed["state"] == "retired"


def test_feedback_scanner_only_proposes_trusted_future_preferences(agent_service):
    from loom.autonomy.profile import scan_feedback

    service, facade, aid, workspace = agent_service
    for index, (producer, message) in enumerate([("human", "以后请输出简洁的中文"), ("agent_planner", "以后请输出简洁的中文"), ("human", "这次输出英文即可")]):
        sid = service.store.create(f"scan-{index}", dict(objective=message, workspace=str(workspace)), producer=producer)["session_id"]
        with service.store.transaction() as db:
            db.execute("INSERT INTO agent_sessions VALUES(?,?,NULL,0)", (sid, aid))
    scan_feedback(facade.store, aid)
    with facade.store.sessions.transaction() as db:
        entries = facade.store.rows(db, "agent_profile", aid)
    assert len(entries) == 1 and entries[0]["state"] == "candidate"
    assert entries[0]["independent_episodes"] == 1
    retired = command(facade.store, aid, dict(command_id="retire-scan", entry_id=entries[0]["id"], type="retire", expected_revision=entries[0]["revision"]))
    scan_feedback(facade.store, aid)
    with facade.store.sessions.transaction() as db:
        assert facade.store.get(db, "agent_profile", retired["id"])["state"] == "retired"
    assert not build_snapshot(facade.store, aid)["preferences"]


def test_adopted_habit_stays_bounded_and_compiles_without_write_or_external_tools(agent_service):
    import asyncio

    from loom.autonomy.dispatch import task_payload
    from loom.autonomy.habits import configure, materialize
    from loom.tasks.assembly import TaskAssembly
    from loom.tasks.request import TaskRequest
    from tests.autonomy.test_goal_iteration import iterative

    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    entry = explicit(aid, facade, key="check", content="Review evidence")
    configure(
        facade.store, aid, dict(command_id="habit", entry_id=entry["id"], expected_revision=entry["revision"], objective_id="quality", every_seconds=3600)
    )
    materialize(facade.store, aid)
    materialize(facade.store, aid)
    snapshot = facade.store.snapshot(aid)
    assert len(snapshot["goals"]) == 1
    goal = snapshot["goals"][0]
    assert goal["read_only"]
    payload = task_payload(snapshot, goal)
    assembly = TaskAssembly(TaskRequest(goal["objective"], task_spec=payload["task_spec"]))
    try:
        assert "read_file" in assembly.bindings
        assert not {"write_file", "edit_file", "shell_execute", "process_execute"} & set(assembly.bindings)
        assert not payload["external_tool_ids"]
    finally:
        asyncio.run(assembly.close())
