import pytest

from loom.autonomy.stage_commands import command
from loom.service.contracts import ServiceError
from tests.autonomy.test_stage_contracts import configure
from tests.autonomy.test_stage_progression import setup_plan


def test_stage_controls_revision_replay_pause_resume_and_skip(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    body = dict(command_id="pause-stage", expected_revision=stage["revision"], type="pause")
    paused = facade.dispatch("POST", [aid, "stages", stage["id"], "commands"], {}, body)
    assert command(facade, aid, stage["id"], body, stage_command=True) == paused
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    assert snap["stages"][0]["state"] == "paused"
    with pytest.raises(ServiceError, match="Revision"):
        command(facade, aid, stage["id"], {**body, "command_id": "stale"}, stage_command=True)
    body = dict(command_id="resume-stage", expected_revision=snap["stages"][0]["revision"], type="resume")
    command(facade, aid, stage["id"], body, stage_command=True)
    snap = facade.store.snapshot(aid)
    assert snap["stages"][0]["state"] == "active"
    command(
        facade,
        aid,
        stage["id"],
        dict(command_id="skip", expected_revision=snap["stages"][0]["revision"], type="skip", reason="Product priorities changed"),
        stage_command=True,
    )
    snap = facade.store.snapshot(aid)
    assert snap["stages"][0]["state"] == "superseded"
    assert next(g for g in snap["goals"] if g.get("stage_id"))["state"] == "cancelled"
    assert not snap["stage_reviews"]


def test_finite_missing_criteria_asks_once_and_rejects_stale_answer(agent_service):
    _, facade, aid, _ = agent_service
    agent = configure(facade, aid)
    raw = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    raw["long_term_objectives"][0]["mode"] = "finite"
    facade.store.revise_role(aid, "finite", agent["revision"], raw)
    facade.supervisor.stages.advance(aid)
    pending = facade.dispatch("GET", [aid, "objectives", "quality", "roadmap"], {})["planning"]
    facade.supervisor.stages.advance(aid)
    assert facade.store.snapshot(aid)["objective_planning"][0] == pending
    question = pending["question"]
    body = dict(
        command_id="answer",
        type="answer",
        expected_revision=pending["revision"],
        question_id=question["id"],
        question_revision=1,
        answer="Both rendering and camera validation pass",
    )
    command(facade, aid, "quality", body)
    assert command(facade, aid, "quality", body)["state"] == "ready"
    with pytest.raises(ServiceError, match="Revision"):
        command(facade, aid, "quality", {**body, "command_id": "stale"})
    facade.supervisor.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["goals"]) == 1


def test_cross_agent_stage_access_rejected_and_config_switch_is_versioned(agent_service):
    _, facade, aid, path = agent_service
    from tests.autonomy.helpers import role

    snap = setup_plan(facade, aid)
    other = facade.store.create("other", role(path))
    with pytest.raises(ServiceError, match="scope"):
        facade.dispatch("GET", [other["id"], "stages", snap["stages"][0]["id"]], {})
    command(facade, aid, "quality", dict(command_id="disable", type="disable", expected_revision=snap["revision"]))
    snap = facade.store.snapshot(aid)
    assert snap["role"]["long_term_objectives"][0]["stage_planning"]["mode"] == "off"
    assert snap["stages"][0]["state"] == "paused"


def test_human_stage_revision_is_preserved_by_replanning(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    command(
        facade,
        aid,
        stage["id"],
        dict(
            command_id="revise",
            type="revise",
            expected_revision=stage["revision"],
            reason="Explicit changed outcome",
            payload=dict(expected_outcome="Camera only", acceptance=["Navigation checks pass"]),
        ),
        stage_command=True,
    )
    facade.supervisor.stages.advance(aid)  # Old fixture proposal does not preserve the requested contract.
    snap = facade.store.snapshot(aid)
    assert snap["objective_planning"][0]["state"] == "blocked"
    assert "human-revised" in snap["objective_planning"][0]["reason"]


def test_role_form_disable_and_enable_pause_same_stage_without_duplicate_delivery(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    for mode, expected in [("off", "paused"), ("autonomous", "active")]:
        raw = {k: v for k, v in snap["role"].items() if k != "schema_version"}
        raw["long_term_objectives"][0]["stage_planning"]["mode"] = mode
        facade.store.revise_role(aid, "switch-" + mode, snap["revision"], raw)
        facade.supervisor.stages.advance(aid)
        snap = facade.store.snapshot(aid)
        assert snap["stages"][0]["state"] == expected
        assert len(snap["stages"]) == 1
