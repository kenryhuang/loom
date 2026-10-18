import asyncio
import threading

import pytest

from loom.service.contracts import ServiceError, canonical
from loom.service.semantic import SessionSemantic
from tests.evaluation.test_goal_loops import GoalJudge
from tests.service.test_semantic import evaluation, wait_job  # noqa: F401


def test_goal_loop_service_scope_partial_resume_and_persistence(evaluation):  # noqa: F811
    store, trajectory, manager, _, sid, aid = evaluation
    judge = GoalJudge()
    manager.provider_factory = lambda _: judge
    cursor = store.snapshot(sid)["event_cursor"]
    options = {"analysis_version": "goal_loops_v1", "run_id": "r", "max_calls": 1}
    started = manager.start(sid, aid, options)
    partial = wait_job(manager, sid, aid, started["id"])
    assert partial["state"] == "budget_exhausted", partial.get("error")
    assert partial["evaluation"]["phases"]
    assert partial["evaluation"]["source"]["run_id"] == "r"
    canonical(partial)  # The polling response must be serializable before publication, too.
    with pytest.raises(ServiceError, match="run"):
        manager.start(sid, aid, {**options, "run_id": "missing"})
    with pytest.raises(ServiceError, match="Select a run"):
        manager.start(sid, aid, {"analysis_version": "goal_loops_v1"})
    resumed = manager.start(sid, aid, {**options, "resume_id": started["id"], "max_calls": 12})
    done = wait_job(manager, sid, aid, resumed["id"])
    assert done["state"] == "completed", done
    assert done["evaluation"]["schema_version"] == "loom.goal-loop-analysis.v1"
    assert done["evaluation"]["coverage"]["pending_packets"] == 0
    assert sum(r["stage"] == "structure:0" for r in judge.requests) == 1
    assert store.snapshot(sid)["event_cursor"] == cursor
    assert manager.list(sid, aid)["run_ids"] == ["r"]
    manager.close()
    restored = SessionSemantic(trajectory, provider_factory=lambda _: judge)
    try:
        assert restored.get(sid, aid, done["id"])["evaluation"] == done["evaluation"]
    finally:
        restored.close()


def test_cancel_keeps_saved_goal_structure_and_does_not_publish_late_result(evaluation):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GoalJudge()
    original = judge.chat
    blocked = threading.Event()

    async def chat(*args, **kwargs):
        if judge.requests:
            blocked.set()
            await asyncio.sleep(60)
        return await original(*args, **kwargs)

    judge.chat = chat
    manager.provider_factory = lambda _: judge
    job = manager.start(sid, aid, {"analysis_version": "goal_loops_v1", "run_id": "r"})
    assert blocked.wait(5)
    manager.cancel(sid, aid, job["id"])
    result = wait_job(manager, sid, aid, job["id"])
    assert result["state"] == "cancelled"
    assert result["evaluation"]["phases"]
    assert result["evaluation"]["coverage"]["status"] == "partial"
    assert result["usage"]["unreported_calls"] == 1


@pytest.mark.parametrize("version", ["1", "2"])
def test_service_resume_upgrades_v1_protocol_without_losing_saved_batches(evaluation, version):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GoalJudge()
    manager.provider_factory = lambda _: judge
    options = {'analysis_version': 'goal_loops_v1', 'run_id': 'r', 'max_calls': 1}
    job = manager.start(sid, aid, options)
    stopped = wait_job(manager, sid, aid, job['id'])
    assert stopped['state'] == 'budget_exhausted'
    old = manager._load(sid, aid, job['id'])
    old.update(analyzer_version='goal-loops.' + version, prompt_version='goal-loops-rubric.' + version)
    old['checkpoint']['identity'].update(analyzer='goal-loops.' + version, prompt='goal-loops-rubric.' + version)
    old['checkpoint']['stage_work']['reconcile:0'] = {'next_round': 2}
    manager._save(old)
    resumed = manager.start(sid, aid, {**options, 'resume_id': job['id'], 'max_calls': 12})
    done = wait_job(manager, sid, aid, resumed['id'])
    assert done['state'] == 'completed', done.get('error')
    assert done['analyzer_version'] == 'goal-loops.3'
    assert done['evaluation']['coverage']['protocol_migrations'] == 1
    assert done['evaluation']['phases'] == stopped['evaluation']['phases']
    assert done['usage']['calls'] == 3
    assert sum(r['stage'].startswith('structure:') for r in judge.requests) == 1


def test_saved_report_gets_read_only_execution_projection_without_new_evaluator_calls(evaluation):  # noqa: F811
    import json
    from copy import deepcopy

    store, _, manager, _, sid, aid = evaluation
    judge = GoalJudge()
    manager.provider_factory = lambda _: judge
    job = manager.start(sid, aid, {'analysis_version':'goal_loops_v1','run_id':'r'})
    done = wait_job(manager, sid, aid, job['id'])
    assert done['state']=='completed',done.get('error')
    old = deepcopy(done['evaluation'])
    old.pop('execution_view')
    old.pop('action_utility')
    ref = store.artifacts.publish(old, 'trajectory_evaluation')
    body = manager._load(sid, aid, job['id'])
    body['result'] = ref
    manager._save(body)
    before = len(judge.requests)
    restored = manager.get(sid, aid, job['id'])['evaluation']
    assert restored['execution_view']['version']=='goal-execution-view.6'
    assert restored['cost_ledger']==old['cost_ledger']
    assert all(call['utility'] is None for call in restored['execution_view']['calls'])
    assert len(judge.requests)==before
    assert 'execution_view' not in json.loads(store.artifacts.read(ref['sha256']))
