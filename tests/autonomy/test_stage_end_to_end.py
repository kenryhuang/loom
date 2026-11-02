from loom.autonomy.acceptance import refs
from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, role, single_package, solver_provider
from tests.autonomy.test_execution_flow import until


def configured_role(workspace):
    configured = role(workspace)
    configured["interval_seconds"] = 21600
    configured["long_term_objectives"][0].update(
        mode="finite", stage_planning=dict(mode="autonomous", completion_criteria=["Camera navigation verified from recorded evidence"])
    )
    return configured


def decision(agent, payload):
    evidence_refs = [e["id"] for e in payload["evidence"]]
    if payload.get("stage"):
        stage = payload["stage"]
        own = [e["id"] for e in payload["evidence"] if e["goal_id"] in stage["goal_ids"]]
        return dict(
            action="review_stage",
            reason="Original criteria verified",
            source_refs=own,
            result="completed",
            checks=[dict(c, source_refs=own) for c in refs(stage["acceptance_criteria"])],
        )
    completed = [s for s in payload["history"] if s["state"] == "completed"]
    if len(completed) == 2:
        return dict(
            action="complete_objective",
            reason="All final criteria have recorded evidence",
            source_refs=evidence_refs,
            checks=[dict(c, source_refs=evidence_refs) for c in refs(payload["objective"]["acceptance_criteria"])],
        )
    title, criterion = (
        ("Document rendering structure", "Rendering structure evidenced")
        if not completed
        else ("Verify camera navigation controls", "Camera navigation verified from recorded evidence")
    )
    return dict(
        action="propose_stage",
        reason="Concrete remaining implementation gap",
        source_refs=evidence_refs,
        baseline="Recorded notes.txt baseline",
        stage=dict(
            title=title,
            covers=refs(payload["objective"].get("acceptance_criteria", [])) if len(completed) == 1 else [],
            expected_outcome=title,
            expected_contribution=title,
            delivery_key=title,
            estimated_cost="One bounded package",
            scope=["Current workspace"],
            non_goals=["Deployment"],
            assumptions=[],
            acceptance=[criterion],
            stop_conditions=["Missing evidence"],
            goals=[dict(key="deliver", objective=title, acceptance=[criterion], depends_on=[])],
        ),
        future=[],
    )


def test_long_term_objective_alone_drives_discovery_two_stages_and_finite_stop(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Session fixture evidence: rendering and camera modules are available.")
    calls = []

    def planner(agent, payload):
        calls.append(payload)
        return decision(agent, payload)

    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=solver_provider,
        agent_planner=single_package,
        agent_stage_planner=planner,
        evaluation_provider_factory=evaluation_provider,
    )
    try:
        facade = service.background.autonomy
        aid = facade.store.create("agent", configured_role(workspace))["id"]
        facade.store.command(aid, "enable", "enable", 1)
        service.start()
        result = until(lambda: facade.snapshot(aid), lambda s: s["objective_planning"] and s["objective_planning"][0]["state"] == "completed", seconds=45)
        assert len(result["stages"]) == 2
        assert all(s["state"] == "completed" for s in result["stages"])
        assert len(result["goals"]) == 3  # Discovery + two stages, no human short-term Goal.
        assert result["goals"][0]["read_only"]
        assert len(calls) == 5  # Two plans, two reviews, one finite completion.
        assert all(p["evidence"][0]["recorded_outputs"] for p in calls)
        assert len(result["learning"]) == 3
        assert result["stages"][1]["predecessor_id"] == result["stages"][0]["id"]
        facade.supervisor.stages.advance(aid)
        assert len(calls) == 5
        until(lambda: facade.snapshot(aid), lambda s: all(c["state"] in {"completed", "incomplete", "deferred"} for c in s["learning"]))
    finally:
        service.close()
