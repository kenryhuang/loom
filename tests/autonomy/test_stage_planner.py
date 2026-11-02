import copy
import json

import pytest

from loom.autonomy.acceptance import criteria, objective_view, refs
from loom.autonomy.stage_planner import validate
from loom.autonomy.stages import evidence
from loom.service.contracts import ServiceError
from tests.autonomy.test_stage_contracts import configure, delivery
from tests.autonomy.test_stage_end_to_end import decision


def payload(facade, aid):
    agent = facade.store.snapshot(aid)
    with facade.store.sessions.transaction() as db:
        records = evidence(facade.store, db, aid, agent["goals"])
    return dict(objective=objective_view(agent, agent["role"]["long_term_objectives"][0]), evidence=records, history=[])


def test_stage_contract_requires_actual_refs_specific_delivery_and_acyclic_goals(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    value = payload(facade, aid)
    valid = decision(None, value)
    assert validate(valid, value) == valid
    for mutate, message in [
        (lambda d: d.update(source_refs=["imaginary"]), "unavailable"),
        (lambda d: d["stage"]["goals"][0].update(objective=value["objective"]["desired_outcome"]), "specific"),
        (lambda d: d["stage"]["goals"][0].update(depends_on=["deliver"]), "/stage/goals/0/depends_on"),
        (lambda d: d.update(future=[dict(key="current", title="a", expected_outcome="b", depends_on=[])]), "dependencies"),
    ]:
        invalid = copy.deepcopy(valid)
        mutate(invalid)
        with pytest.raises(ServiceError, match=message):
            validate(invalid, value)


def test_wait_does_not_poll_model_until_new_input_or_review_due(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    calls = []

    def wait(agent, value):
        calls.append(value)
        return dict(action="wait", reason="No meaningful gap remains", source_refs=[])

    facade.supervisor.stages.planner.provider = wait
    facade.supervisor.stages.advance(aid)
    for _ in range(5):
        facade.supervisor.stages.advance(aid)
    assert len(calls) == 1
    facade.store.clock.advance(86401)
    facade.supervisor.stages.advance(aid)
    # A due review first refreshes the now-stale baseline through a real Goal.
    discovery = facade.store.snapshot(aid)["goals"][-1]
    assert discovery["producer"] == "stage_discovery"
    assert len(calls) == 1
    delivery(facade, aid, discovery)
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 2


def test_budget_refusal_defers_without_replanning_each_tick(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    calls = []

    def refuse(agent, value):
        calls.append(value)
        raise ServiceError("Agent daily token budget exhausted", 409, code="AGENT_BUDGET")

    facade.supervisor.stages.planner.provider = refuse
    facade.supervisor.stages.advance(aid)
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 1
    assert facade.store.snapshot(aid)["objective_planning"][0]["state"] == "deferred"
    facade.store.clock.advance(86400)
    facade.supervisor.stages.advance(aid)
    assert len(calls) == 2


def test_stage_model_format_correction_is_metered_before_goal_exists(agent_service, monkeypatch):
    import json

    from loom.autonomy import stage_planner as module
    from loom.core import ok
    from tests.autonomy.test_planner import ReplyProvider, reply

    service, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    value = payload(facade, aid)
    provider = ReplyProvider([reply("invalid"), reply(json.dumps(decision(None, value)))])
    service.config_path = "fixture.yaml"
    monkeypatch.setattr(module, "load_task_config", lambda _: ok(None))
    monkeypatch.setattr(module, "_create_provider", lambda *_args, **_kwargs: ok(provider))
    facade.supervisor.stages.advance(aid)
    snap = facade.snapshot(aid)
    assert len(snap["stages"]) == 1
    usage = next(r for r in snap["budget"] if r["category"] == "stage_planning")
    assert usage["calls"] == 2 and usage["tokens"] == 60
    assert snap["objective_budget"]["objectives"][0]["actual_tokens"] == 60


def test_old_deliveries_trigger_one_fresh_baseline_investigation(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    facade.store.clock.advance(86401)
    facade.supervisor.stages.planner.provider = lambda *args: pytest.fail("Stale evidence needs revalidation")
    facade.supervisor.stages.advance(aid)
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    assert len(snap["goals"]) == 2
    assert snap["goals"][-1]["producer"] == "stage_discovery"


def model_fixture(agent_service, monkeypatch):
    from loom.autonomy import stage_planner as module
    from loom.core import ok
    from tests.autonomy.test_planner import ReplyProvider

    service, facade, aid, _ = agent_service
    configure(facade, aid)
    delivery(facade, aid)
    value = payload(facade, aid)
    valid = decision(None, value)
    valid["future"] = [dict(key="next", title="Next delivery", expected_outcome="Verified next delivery", depends_on=["current"])]
    provider = ReplyProvider([])
    service.config_path = "fixture.yaml"
    monkeypatch.setattr(module, "load_task_config", lambda _: ok(None))
    monkeypatch.setattr(module, "_create_provider", lambda *_args, **_kwargs: ok(provider))
    return facade, aid, provider, valid


@pytest.mark.parametrize(
    "mutate,diagnosis",
    [
        (lambda d: d["future"][0].update(depends_on=[d["stage"]["delivery_key"]]), "allowed earlier keys"),
        (lambda d: d["future"][0].update(key="current"), "reserved"),
        (lambda d: d["future"][0].update(depends_on=["next"]), "self or later"),
        (lambda d: d["future"][0].update(depends_on=["current", "current"]), "duplicate entries"),
        (lambda d: d["stage"]["goals"][0].update(depends_on=["deliver"]), "/stage/goals/0/depends_on"),
        (lambda d: d.update(source_refs=["fabricated"]), "unavailable evidence"),
    ],
)
def test_semantic_rejection_is_corrected_metered_and_auditable(agent_service, monkeypatch, mutate, diagnosis):
    from tests.autonomy.test_planner import reply

    facade, aid, provider, valid = model_fixture(agent_service, monkeypatch)
    invalid = copy.deepcopy(valid)
    mutate(invalid)
    provider.replies = iter([reply(json.dumps(invalid)), reply(json.dumps(valid))])
    facade.supervisor.stages.advance(aid)
    snap = facade.snapshot(aid)
    assert len(snap["stages"]) == 1
    assert len(snap["goals"]) == 2
    assert len(provider.payloads) == 2
    repair = provider.payloads[1]["repair"]
    assert diagnosis in repair["validation_error"]
    assert repair["previous_proposal"] == invalid
    assert provider.payloads[1]["objective"] == provider.payloads[0]["objective"]
    assert provider.payloads[1]["evidence"] == provider.payloads[0]["evidence"]
    usage = next(r for r in snap["budget"] if r["category"] == "stage_planning")
    assert usage["calls"] == 2 and usage["tokens"] == 60
    with facade.store.sessions.transaction() as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_events WHERE agent_id=? ORDER BY seq", (aid,))]
    proposed = [e["payload"] for e in events if e["type"] == "stage.protocol.proposed"]
    assert len(proposed) == 2
    assert facade.store.read_artifact(aid, proposed[0]["proposal_ref"]["sha256"]) == invalid
    assert any(e["type"] == "stage.protocol.rejected" and e["payload"]["code"] == "invalid_domain" for e in events)


@pytest.mark.parametrize("budget_limited", [False, True])
def test_semantic_correction_is_bounded_without_partial_commits(agent_service, monkeypatch, budget_limited):
    from tests.autonomy.test_planner import reply

    facade, aid, provider, invalid = model_fixture(agent_service, monkeypatch)
    invalid["future"][0]["depends_on"] = ["unknown"]
    provider.replies = iter([reply(json.dumps(invalid))] * 3)
    if budget_limited:
        with facade.store.sessions.transaction() as db:
            agent = facade.store.get(db, "agents", aid)
            agent["role"]["budget"]["max_calls"] = 1
            facade.store.put(db, "agents", agent)
    facade.supervisor.stages.advance(aid)
    facade.supervisor.stages.advance(aid)
    snap = facade.snapshot(aid)
    assert snap["stages"] == [] and snap["roadmaps"] == []
    assert len(snap["goals"]) == 1 and len(snap["work_packages"]) == 1  # Only the existing baseline.
    assert len(provider.payloads) == (1 if budget_limited else 2)
    assert snap["objective_planning"][0]["state"] == ("deferred" if budget_limited else "blocked")
    with facade.store.sessions.transaction() as db:
        run = facade.store.rows(db, "agent_runs", aid)[-1]
    assert run["state"] == "failed" and "decision" not in run


@pytest.mark.parametrize("outcome", ["corrected", "exhausted", "budget"])
def test_review_contract_citation_correction_is_metered_bounded_and_preserves_stage(agent_service, monkeypatch, outcome):
    from tests.autonomy.test_planner import reply

    facade, aid, provider, plan = model_fixture(agent_service, monkeypatch)
    plan["stage"]["goals"].insert(0, dict(
        key="component", objective="Verify a component before integration", acceptance=["Component tests pass"], depends_on=[],
    ))
    provider.replies = iter([reply(json.dumps(plan))])
    facade.supervisor.stages.advance(aid)
    snap = facade.store.snapshot(aid)
    stage = snap["stages"][0]
    for goal in snap["goals"]:
        if goal["id"] in stage["goal_ids"]:
            delivery(facade, aid, goal)
    value = payload(facade, aid)
    value["stage"] = stage
    valid = decision(None, value)
    invalid = copy.deepcopy(valid)
    component_ref = "delivery:work-" + stage["goal_ids"][0]
    integration_ref = "delivery:work-" + stage["goal_ids"][-1]
    invalid["checks"][0]["source_refs"] = [component_ref]
    provider.payloads.clear()
    provider.replies = iter([reply(json.dumps(invalid)), reply(json.dumps(valid if outcome == "corrected" else invalid))])
    if outcome == "budget":
        with facade.store.sessions.transaction() as db:
            agent = facade.store.get(db, "agents", aid)
            agent["role"]["budget"]["max_calls"] = 2  # Stage plan plus initial review; no room for correction.
            facade.store.put(db, "agents", agent)
    facade.supervisor.stages.advance(aid)
    if outcome != "corrected":
        facade.supervisor.stages.advance(aid)  # A timer tick cannot repeatedly pay for rejected responses.
    snap = facade.snapshot(aid)
    assert len(snap["stages"]) == 1 and snap["stages"][0]["id"] == stage["id"]
    assert len(snap["goals"]) == 3 and all(g["state"] == "completed" for g in snap["goals"])
    assert provider.payloads[0]["acceptance_support"] == [dict(refs(stage["acceptance_criteria"])[0],
                                                              description=stage["acceptance"][0], source_refs=[integration_ref])]
    assert len(provider.payloads) == (1 if outcome == "budget" else 2)
    if outcome != "budget":
        error = provider.payloads[1]["repair"]["validation_error"]
        assert "No passed execution contract" in error and stage["acceptance_criteria"][0]["id"] in error and integration_ref in error
        assert provider.payloads[1]["repair"]["previous_proposal"] == invalid
        assert provider.payloads[1]["evidence"] == provider.payloads[0]["evidence"]
    usage = next(r for r in snap["budget"] if r["category"] == "stage_planning")
    assert usage["calls"] == (2 if outcome == "budget" else 3)
    assert usage["tokens"] == (60 if outcome == "budget" else 90)
    if outcome == "corrected":
        assert snap["stages"][0]["state"] == "completed"
        assert snap["stage_reviews"][0]["decision"] == valid
    else:
        assert snap["stage_reviews"] == [] and snap["stages"][0]["state"] == "reviewing"
        assert snap["objective_planning"][0]["state"] == ("deferred" if outcome == "budget" else "blocked")
        with facade.store.sessions.transaction() as db:
            run = facade.store.rows(db, "agent_runs", aid)[-1]
        assert run["state"] == "failed" and "decision" not in run


@pytest.mark.parametrize("action", ["review_stage", "complete_objective"])
@pytest.mark.parametrize("invalid_evidence", ["failed", "partial", "observation"])
def test_completion_cannot_use_unpassed_or_partial_evidence_even_with_supplied_support(action, invalid_evidence):
    criterion = "Tests, build and lint pass"
    definitions = criteria("stage", [criterion])
    evidence_row = dict(id="delivery:integration", kind="delivery", goal_id="goal", passed=True,
                        acceptance=[criterion], verified_criteria=refs(definitions))
    if invalid_evidence == "failed":
        evidence_row["passed"] = False
    elif invalid_evidence == "partial":
        evidence_row["acceptance"] = ["Tests pass", "Build and lint pass"]
        evidence_row["verified_criteria"] = refs(criteria("partial", evidence_row["acceptance"]))
    else:
        evidence_row["kind"] = "observation"
    value = dict(
        objective=dict(mode="finite", stage_planning=dict(completion_criteria=[criterion]), acceptance_criteria=definitions),
        evidence=[evidence_row],
        acceptance_support=[dict(refs(definitions)[0], source_refs=[evidence_row["id"]])],
    )
    if action == "review_stage":
        value["stage"] = dict(acceptance=[criterion], acceptance_criteria=definitions, goal_ids=["goal"])
    proposal = dict(action=action, reason="Claimed completion", result="completed", source_refs=[evidence_row["id"]],
                    checks=[dict(refs(definitions)[0], source_refs=[evidence_row["id"]])])
    with pytest.raises(ServiceError, match="eligible source_refs=\\[\\]"):
        validate(proposal, value)
