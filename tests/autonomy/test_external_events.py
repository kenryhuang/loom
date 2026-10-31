from loom.autonomy.events import ingest
from loom.autonomy.goals import materialize
from tests.autonomy.test_goal_iteration import iterative


def test_bound_events_reorder_reconnect_and_self_loop_suppression(agent_service):
    _, facade, aid, _ = agent_service
    iterative(facade, aid)
    a = facade.store.snapshot(aid)
    role = {k: v for k, v in a["role"].items() if k != "schema_version"}
    role["source_routes"][0]["objective_id"] = "quality"
    facade.store.revise_role(aid, "bind-event", a["revision"], role)

    def event(version, **changes):
        return dict(
            command_id=f"event-{version}",
            event_id=f"event-{version}",
            source_id="code",
            object_id="src",
            version=version,
            occurred_at="2026-10-30T00:00:00+00:00",
            cursor=str(version),
            origin="connector",
            type="changed",
            summary="Source updated",
            **changes,
        )

    first = ingest(facade.store, aid, event(2))
    assert ingest(facade.store, aid, event(2)) == first
    assert ingest(facade.store, aid, event(1))["state"] == "out_of_order"
    materialize(facade.store, aid)
    assert len(facade.store.snapshot(aid)["goals"]) == 1
    own = event(3)
    own["origin"] = aid
    assert ingest(facade.store, aid, own)["state"] == "suppressed"
    materialize(facade.store, aid)
    assert len(facade.store.snapshot(aid)["goals"]) == 1
    revoked = event(4)
    revoked["type"] = "revoked"
    ingest(facade.store, aid, revoked)
    assert facade.store.snapshot(aid)["goals"][0]["state"] == "superseded"
