import pytest

from loom.autonomy.goals import materialize, observe, rank, retry
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import Clock


def iterative(facade, aid):
    facade.store.clock = Clock()
    agent = facade.store.snapshot(aid)
    role = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    role["long_term_objectives"][0]["iteration"] = dict(acceptance=["Evidence checked"], max_depth=2)
    return facade.store.revise_role(aid, "iteration-role", agent["revision"], role)


def test_evidence_driven_goals_dedupe_unknown_and_finite_completion(agent_service):
    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    store = facade.store
    row = observe(store, aid, "observe", dict(objective_id="quality", indicator="coverage", state="unknown", source="human review", object_id="code"))
    materialize(store, aid)
    materialize(store, aid)
    snap = store.snapshot(aid)
    assert len(snap["goals"]) == 1
    assert snap["goals"][0]["proposal"]["observation_id"] == row["id"]
    assert snap["objective_states"]["quality"]["state"] == "unknown"
    observe(store, aid, "met", dict(objective_id="quality", indicator="coverage", state="met", source="acceptance", object_id="code"))
    materialize(store, aid)
    assert store.snapshot(aid)["objective_states"]["quality"]["state"] == "met"


def test_retry_requires_changed_input_backoff_and_keeps_failure(agent_service):
    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    store = facade.store
    goal = store.add_goal(aid, "goal", dict(objective="Verify", acceptance=["passed"]))
    with store.sessions.transaction() as db:
        goal.update(state="blocked")
        store.put(db, "agent_goals", goal)
    body = dict(command_id="retry", expected_revision=1, change="Input now includes output", change_kind="input")
    new = retry(store, aid, goal["id"], body)
    assert retry(store, aid, goal["id"], body) == new
    with store.sessions.transaction() as db:
        candidates, reasons = rank(store, db, store.get(db, "agents", aid), store.rows(db, "agent_goals", aid), [])
    assert not candidates and "backoff" in reasons[-1]["reason"]
    store.clock.advance(61)
    with store.sessions.transaction() as db:
        candidates, _ = rank(store, db, store.get(db, "agents", aid), store.rows(db, "agent_goals", aid), [])
    assert candidates[0]["id"] == new["id"]
    with pytest.raises(ServiceError, match="already"):
        retry(store, aid, goal["id"], {**body, "command_id": "duplicate"})
    assert store.snapshot(aid)["goals"][0]["state"] == "blocked"


def test_ongoing_review_waits_for_due_time_and_finite_goal_does_not_reopen(agent_service):
    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    store = facade.store
    observe(store, aid, "met-now", dict(objective_id="quality", indicator="coverage", state="met", source="inspection"))
    materialize(store, aid)
    assert not store.snapshot(aid)["goals"]
    store.clock.advance(86401)
    materialize(store, aid)
    assert len(store.snapshot(aid)["goals"]) == 1
    materialize(store, aid)
    assert len(store.snapshot(aid)["goals"]) == 1


def test_new_routes_use_versioned_profile_and_event_api(agent_service):
    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    row = facade.dispatch(
        "POST",
        [aid, "observations"],
        {},
        dict(command_id="via-api", observation=dict(objective_id="quality", indicator="coverage", state="gap", source="human inspection")),
    )
    assert row["producer"] == "human"
    assert facade.dispatch("GET", [aid, "observations"], {})["observations"][0]["id"] == row["id"]
    facade.dispatch("POST", [aid, "preferences", "feedback"], {}, dict(command_id="prefer-api", key="format", text="Concise", kind="explicit"))
    assert facade.snapshot(aid)["preferences"][0]["text"] == "Concise"
