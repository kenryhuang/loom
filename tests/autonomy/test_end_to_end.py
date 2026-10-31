import json

from loom.memory.annotations import annotate
from loom.memory.store import MemoryStore
from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, gated_solver_provider, goals, role, single_package
from tests.autonomy.test_execution_flow import until


def test_running_guidance_annotation_and_action_evidence_at_next_request(tmp_path):
    (tmp_path / "notes.txt").write_text("Session fixture evidence: this observation belongs to the current workspace.")
    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=gated_solver_provider,
        evaluation_provider_factory=evaluation_provider,
        agent_planner=single_package,
    )
    try:
        facade = service.background.autonomy
        aid = facade.store.create("create", role(tmp_path))["id"]
        facade.store.add_goal(aid, "first", goals()[0])
        facade.control(aid, dict(command_id="enable", type="enable", expected_revision=1))
        service.start()
        first = until(lambda: facade.snapshot(aid), lambda s: s["learning"] and s["learning"][0]["state"] == "completed")
        record = MemoryStore(service.store.path, workspace=tmp_path).search("Session fixture")["records"][0]
        facade.store.add_goal(aid, "second", {**goals()[1], "objective": "Session boundary-gate review"})
        until(lambda: (tmp_path / "request-pending").exists(), bool)
        guide = facade.guidance(
            aid,
            dict(command_id="guide", method_text="Use the corrected scope and check file evidence.", scope="persistent", title="Evidence method"),
            method=True,
        )
        source = record["source"]
        annotate(
            facade.store,
            aid,
            "scope",
            dict(
                evaluation_id=source["evaluation_id"],
                record_id=source["record_id"],
                content_digest=source["content_digest"],
                expected_revision=0,
                kind="scope",
                value="Current fixture workspace only",
            ),
        )
        (tmp_path / "release-request").touch()
        second = until(lambda: facade.snapshot(aid), lambda s: len(s["learning"]) == 2 and s["learning"][-1]["state"] in {"completed", "failed"})
        assert second["goals"][-1]["state"] == "completed", second
        assert second["learning"][-1]["state"] == "completed"
        sid = second["sessions"][0]["session_id"]
        rid = second["work_packages"][-1]["run_ids"][0]
        assert len(second["sessions"]) == 1
        assert rid in next(g for g in second["guidance"] if g["id"] == guide["id"])["applied_run_ids"]
        events = service.events(sid, limit=1000)
        updates = [e for e in events if e["run_id"] == rid and e["type"] == "context.updated"]
        assert any(guide["id"] in e["payload"]["guidance_ids"] for e in updates)
        with service.store.transaction() as db:
            uses = [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_memory_uses WHERE run_id=?", (rid,))]
        assert any(u["kind"] == "model_reported_use" and u["action_refs_validated"] for u in uses), uses
        assert all(u.get("causal_effect") != "improved" for u in uses)
        contexts = facade.dispatch("GET", [aid, "context"], {})["context"]
        actual = [facade.store.read_artifact(aid, c["artifact_ref"]["sha256"]) for c in contexts if c["run_id"] == rid]
        assert any("Current fixture workspace only" in json.dumps(c) for c in actual)
        assert first["learning"][0]["terminal_event_cursor"] < second["learning"][1]["terminal_event_cursor"]
    finally:
        (tmp_path / "release-request").touch()
        service.close()


def test_failed_run_publishes_lesson_without_claiming_success(tmp_path):
    from tests.autonomy.helpers import failed_solver_provider

    (tmp_path / "notes.txt").write_text("Session fixture evidence: known available source.")
    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=failed_solver_provider,
        evaluation_provider_factory=evaluation_provider,
        agent_planner=single_package,
    )
    try:
        facade = service.background.autonomy
        aid = facade.store.create("create", role(tmp_path))["id"]
        facade.store.add_goal(aid, "fail", {**goals()[0], "objective": "Session failure-fixture inspection"})
        facade.control(aid, dict(command_id="enable", type="enable", expected_revision=1))
        service.start()
        first = until(lambda: facade.snapshot(aid), lambda s: s["learning"] and s["learning"][0]["state"] in {"completed", "failed"}, seconds=45)
        assert first["work_packages"][0]["state"] == "failed"
        assert first["learning"][0]["state"] == "completed", first["learning"]
        memories = MemoryStore(service.store.path, workspace=tmp_path).search("Session missing-file")["records"]
        assert any(r["kind"] == "lessons" for r in memories), memories
        facade.store.add_goal(aid, "successor", goals()[1])
        next_run = until(lambda: facade.snapshot(aid), lambda s: len(s["goals"]) == 2 and s["goals"][-1]["state"] == "completed")
        work = next_run["work_packages"][-1]
        events = service.events(work["session_id"], limit=1000)
        assert any(e["type"] == "memory.retrieved" and e["payload"].get("sources") for e in events)
        assert any(e["type"] == "tool.completed" and "notes.txt" in json.dumps(e) for e in events)
    finally:
        service.close()
