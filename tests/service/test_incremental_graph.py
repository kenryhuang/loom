import asyncio
import threading

from loom.service.semantic import SessionSemantic
from tests.evaluation.test_incremental_graph import GraphJudge
from tests.service.test_semantic import evaluation, wait_job  # noqa: F401


def test_incremental_service_checkpoint_resume_artifact_and_legacy_scope(evaluation):  # noqa: F811
    store, trajectory, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    manager.provider_factory = lambda _: judge
    cursor = store.snapshot(sid)["event_cursor"]
    assert manager.list(sid, aid)["default_analysis_version"] == "goal_graph_v1"
    options = dict(analysis_version="goal_graph_v1", run_id="r", batch_rounds=1, max_calls=1, max_tokens=100)
    started = manager.start(sid, aid, options)
    partial = wait_job(manager, sid, aid, started["id"])
    assert partial["state"] == "budget_exhausted", partial.get("error")
    first_revision = partial["evaluation"]["graph_revision"]
    assert first_revision > 0
    assert partial["evaluation"]["coverage"]["pending_cards"] == 1
    # A saved .2 checkpoint can resume under the uncapped output policy.
    saved = manager._load(sid, aid, started["id"])
    saved["prompt_version"] = saved["checkpoint"]["identity"]["prompt"] = "goal-graph-rubric.2"
    saved["analyzer_version"] = saved["checkpoint"]["identity"]["analyzer"] = "goal-graph.2"
    manager._save(saved)
    resumed = manager.start(sid, aid, {**options, "resume_id": started["id"], "max_calls": 10})
    done = wait_job(manager, sid, aid, resumed["id"])
    assert done["state"] == "completed", done.get("error")
    result = done["evaluation"]
    assert result["graph_revision"] == result["report"]["graph_revision"] > first_revision
    assert result["cost_ledger"]["conserved"]
    assert len(judge.requests) == 2
    assert done["usage"]["total_tokens"] > done["settings"]["max_tokens"]
    assert manager._load(sid, aid, done["id"])["checkpoint"]["protocol_upgrades"][0]["from_prompt"] == "goal-graph-rubric.2"
    assert store.snapshot(sid)["event_cursor"] == cursor
    manager.close()
    restored = SessionSemantic(trajectory, provider_factory=lambda _: judge)
    try:
        assert restored.get(sid, aid, done["id"])["evaluation"] == result
        assert len(judge.requests) == 2
    finally:
        restored.close()


def test_incremental_cancel_retains_live_graph(evaluation):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    original = judge.chat
    blocked = threading.Event()

    async def chat(*args, **kwargs):
        if judge.requests:
            blocked.set()
            await asyncio.sleep(60)
        return await original(*args, **kwargs)

    judge.chat = chat
    manager.provider_factory = lambda _: judge
    job = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r", batch_rounds=1))
    assert blocked.wait(5)
    manager.cancel(sid, aid, job["id"])
    stopped = wait_job(manager, sid, aid, job["id"])
    assert stopped["state"] == "cancelled"
    assert stopped["evaluation"]["graph_revision"] > 0
    assert stopped["evaluation"]["coverage"]["status"] == "partial"
    assert stopped["usage"]["unreported_calls"] == 1
