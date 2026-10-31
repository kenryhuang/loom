import time

from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, goals, role, single_package, solver_provider


def until(function, predicate, seconds=35):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = function()
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def test_real_worker_two_related_runs_and_learning(tmp_path):
    (tmp_path / "notes.txt").write_text("Session fixture evidence: workspace cannot be changed in place.\n")
    service = LoomService(
        tmp_path / "data",
        autonomy_enabled=True,
        provider_factory=solver_provider,
        agent_planner=single_package,
        evaluation_provider_factory=evaluation_provider,
    )
    try:
        facade = service.background.autonomy
        agent = facade.store.create("create", role(tmp_path))
        aid = agent["id"]
        facade.store.add_goal(aid, "goal-1", goals()[0])
        facade.control(aid, dict(command_id="enable", type="enable", expected_revision=1))
        service.start()
        first = until(lambda: facade.snapshot(aid), lambda s: s["goals"][0]["state"] in {"completed", "blocked"})
        assert first["goals"][0]["state"] == "completed", first
        learned = until(lambda: facade.snapshot(aid), lambda s: s["learning"] and s["learning"][0]["state"] in {"completed", "failed", "deferred"})
        assert learned["learning"][0]["state"] == "completed", learned["learning"]
        assert learned["learning"][0]["memory_result"]["accepted"] >= 1, learned["learning"]
        facade.guidance(aid, dict(command_id="guide", text="以后区分已实现与拟议能力", scope="persistent"))
        facade.store.add_goal(aid, "goal-2", goals()[1])
        second = until(lambda: facade.snapshot(aid), lambda s: len(s["goals"]) == 2 and s["goals"][1]["state"] in {"completed", "blocked"})
        assert second["goals"][1]["state"] == "completed", second
        assert len(second["sessions"]) == 1
        packages = second["work_packages"]
        assert packages[0]["run_ids"] != packages[1]["run_ids"]
        sid = second["sessions"][0]["session_id"]
        events = service.events(sid, limit=1000)
        assert any(e["type"] == "context.updated" and e["run_id"] == packages[1]["run_ids"][0] for e in events)
        assert any(e["type"] == "memory.retrieved" and e["payload"].get("sources") for e in events)
        assert all(m.get("producer") == "agent_planner" for m in service.store.snapshot(sid)["messages"] if m["role"] == "user")
    finally:
        service.close()
