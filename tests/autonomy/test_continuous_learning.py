from loom.autonomy.method_feedback import report
from loom.autonomy.profile import feedback
from loom.autonomy.sources import refresh
from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, role, single_package, solver_provider
from tests.autonomy.test_execution_flow import until


def test_source_goal_execution_learning_preference_and_next_cycle(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    note = workspace / "notes.txt"
    note.write_text("Session fixture evidence: version one")
    configured = role(workspace)
    configured["long_term_objectives"][0]["desired_outcome"] = "Review Session fixture evidence and design boundaries"
    configured["source_routes"] = [dict(id="notes", topic="recorded evidence", paths=["notes.txt"], objective_id="quality")]
    configured["long_term_objectives"][0]["iteration"] = dict(acceptance=["Read notes.txt and distinguish facts from proposals"])
    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=solver_provider,
        agent_planner=single_package,
        evaluation_provider_factory=evaluation_provider,
    )
    try:
        facade = service.background.autonomy
        aid = facade.store.create("agent", configured)["id"]
        facade.store.command(aid, "enable", "enable", 1)
        refresh(facade.store, aid, force=True)
        service.start()
        first = until(lambda: facade.snapshot(aid), lambda s: s["learning"] and s["learning"][0]["state"] == "completed")
        assert first["goals"][0]["producer"] == "goal_proposal"
        feedback(facade.store, aid, dict(command_id="correction", key="language", text="Use Chinese in future", kind="explicit"))
        # A check records the Agent's completed own work without generating a feedback loop.
        refresh(facade.store, aid, force=True)
        note.write_text("Session fixture evidence: external version two")
        refresh(facade.store, aid, force=True)
        second = until(lambda: facade.snapshot(aid), lambda s: len(s["learning"]) == 2 and s["learning"][-1]["state"] == "completed")
        rid = second["work_packages"][-1]["run_ids"][-1]
        with facade.store.sessions.transaction() as db:
            contexts = facade.store.rows(db, "agent_contexts", aid)
        snapshots = [facade.store.read_artifact(aid, c["artifact_ref"]["sha256"]) for c in contexts if c["run_id"] == rid]
        assert any(s["preferences"][0]["text"] == "Use Chinese in future" for s in snapshots)
        assert any(s["memory"]["records"] for s in snapshots)
        assert report(facade.store, aid)["causal_effect"] == "unknown"
        note.unlink()
        assert refresh(facade.store, aid, force=True)[0]["state"] == "unavailable"
    finally:
        service.close()
