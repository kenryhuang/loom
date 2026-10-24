import asyncio
import threading

import pytest

from loom.evaluation.goal_loops.facts import identity
from loom.service.contracts import ServiceError
from loom.service.semantic import SessionSemantic
from tests.evaluation.test_incremental_graph import GraphJudge
from tests.service.test_semantic import evaluation, wait_job  # noqa: F401


@pytest.mark.parametrize("old_prompt,old_analyzer", [
    ("goal-graph-rubric.2", "goal-graph.2"), ("goal-graph-rubric.4", "goal-graph.3"), ("goal-graph-rubric.5", "goal-graph.3"),
    ("goal-graph-rubric.6", "goal-graph.3"), ("goal-graph-rubric.7", "goal-graph.3"),
])
def test_incremental_service_checkpoint_resume_artifact_and_legacy_scope(evaluation, old_prompt, old_analyzer):  # noqa: F811
    store, trajectory, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    manager.provider_factory = lambda _: judge
    cursor = store.snapshot(sid)["event_cursor"]
    catalog = manager.list(sid, aid)
    assert catalog["default_analysis_version"] == "goal_graph_v1"
    assert catalog["defaults_by_version"]["goal_graph_v1"]["max_seconds"] == 3600
    assert catalog["defaults_by_version"]["goal_graph_v1"]["max_evidence_chars"] == 524288
    assert catalog["defaults_by_version"]["goal_graph_v1"]["max_prompt_chars"] == 524288
    assert catalog["defaults_by_version"]["goal_graph_v1"]["max_calls"] == 48
    assert catalog["defaults_by_version"]["goal_graph_v1"]["max_call_seconds"] == 180
    options = dict(analysis_version="goal_graph_v1", run_id="r", batch_rounds=1, max_calls=1, max_tokens=100)
    started = manager.start(sid, aid, options)
    partial = wait_job(manager, sid, aid, started["id"])
    assert partial["evaluation"]["source"]["session_id"] == sid
    assert partial["evaluation"]["report"]["text"].count(f"Session: {sid}") == 1
    assert partial["state"] == "budget_exhausted", partial.get("error")
    first_revision = partial["evaluation"]["graph_revision"]
    assert first_revision > 0
    assert partial["evaluation"]["coverage"]["pending_cards"] == 1
    # A saved .2 checkpoint can resume under the uncapped output policy.
    saved = manager._load(sid, aid, started["id"])
    saved["prompt_version"] = saved["checkpoint"]["identity"]["prompt"] = old_prompt
    saved["analyzer_version"] = saved["checkpoint"]["identity"]["analyzer"] = old_analyzer
    manager._save(saved)
    resumed = manager.start(sid, aid, {**options, "resume_id": started["id"], "max_calls": 10})
    done = wait_job(manager, sid, aid, resumed["id"])
    assert done["state"] == "completed", done.get("error")
    result = done["evaluation"]
    assert result["graph_revision"] == result["report"]["graph_revision"] > first_revision
    assert result["cost_ledger"]["conserved"]
    assert len(judge.requests) == 2
    assert done["usage"]["total_tokens"] > done["settings"]["max_tokens"]
    assert manager._load(sid, aid, done["id"])["checkpoint"]["protocol_upgrades"][0]["from_prompt"] == old_prompt
    assert result["analyzer"]["input_policy"]["source_reads"] is False
    assert store.snapshot(sid)["event_cursor"] == cursor
    manager.close()
    restored = SessionSemantic(trajectory, provider_factory=lambda _: judge)
    try:
        assert restored.get(sid, aid, done["id"])["evaluation"] == result
        assert len(judge.requests) == 2
    finally:
        restored.close()


@pytest.mark.parametrize("stop_kind", ["cancel", "shutdown"])
def test_incremental_cancel_retains_live_graph(evaluation, stop_kind):  # noqa: F811
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
    if stop_kind == "shutdown":
        manager.closed.set()
    else:
        manager.cancel(sid, aid, job["id"])
    stopped = wait_job(manager, sid, aid, job["id"])
    expected = "interrupted" if stop_kind == "shutdown" else "cancelled"
    assert stopped["state"] == expected
    assert stopped["current_call"]["state"] == expected
    assert stopped["current_call"]["error_code"] == "runtime." + expected
    assert stopped["call_history"][-1] == stopped["current_call"]
    assert stopped["error_details"][0]["code"] == "runtime." + expected
    event = next(row for row in stopped["progress"] if row["kind"] == "call." + expected)
    assert ("shutting down" if stop_kind == "shutdown" else "Cancellation requested") in event["message"]
    assert not any(row["kind"] == "call.failed" for row in stopped["progress"])
    assert stopped["evaluation"]["graph_revision"] > 0
    assert stopped["evaluation"]["coverage"]["status"] == "partial"
    assert stopped["usage"]["unreported_calls"] == 1


def test_incomplete_graph_job_reports_issue_detail_and_resume_hint(evaluation):  # noqa: F811
    from tests.evaluation.test_goal_structure import LongReasonJudge

    _, _, manager, _, sid, aid = evaluation
    judge = LongReasonJudge("single_goal")
    manager.provider_factory = lambda _: judge
    job = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r"))
    stopped = wait_job(manager, sid, aid, job["id"])
    assert stopped["state"] == "incomplete"
    assert "goal-structure windows pending" in stopped["error"]
    assert "at most 240 characters" in stopped["error"]
    assert "Resume evaluation" in stopped["error"]
    detail = next(e for e in stopped["error_details"] if e["code"] == "schema.maxLength")
    assert detail["unit_id"].startswith("batch:")
    assert detail["record_type"] == "structure" and detail["path"] == ["reason"]
    assert detail["details"]["limit"] == 240


def test_host_exception_reports_internal_error_without_model_repair(evaluation, monkeypatch):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    manager.provider_factory = lambda _: judge

    def broken(*args, **kwargs):
        raise KeyError("test-invariant")

    monkeypatch.setattr("loom.evaluation.goal_loops.graph_updates.commit_group", broken)
    job = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r"))
    result = wait_job(manager, sid, aid, job["id"])
    assert result["state"] == "failed"
    assert len(judge.requests) == 1
    assert result["error_details"][0]["code"] == "runtime.internal"
    assert "KeyError" in result["error"] and "test-invariant" in result["error"]


def test_memory_failure_exposes_structured_cause_and_retains_completed_graph(evaluation):  # noqa: F811
    import json

    from loom.core import ok
    from loom.llm import LlmResponse

    _, _, manager, _, sid, aid = evaluation

    class InvalidMemory(GraphJudge):
        memory_calls = 0

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            payload = json.loads(messages[1].content)
            if payload.get("mode") != "extract":
                return await super().chat(messages, tools, cancellation, **kwargs)
            self.memory_calls += 1
            rows = dict(contributions=[], facts=[], lessons=[], workflows=[])
            rows["contributions"] = [dict(stage_id=s["id"], goal_id=s["goal_id"], rating="unknown", rationale="Unknown contribution",
                                           key_action_ids=[], citations=[dict(ref="not-delivered", quote="Invented")]) for s in payload["stages"]]
            return ok(LlmResponse(content=json.dumps(rows)))

    judge = InvalidMemory()
    manager.provider_factory = lambda _: judge
    job = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r", extract_memory=True))
    result = wait_job(manager, sid, aid, job["id"])
    assert result["state"] == "incomplete", result.get("error")
    assert judge.memory_calls == 2
    error = result["error_details"][0]
    assert error["code"] == "memory.citation_undelivered"
    assert error["path"] == ["citations", 0, "ref"] and error["record_index"] == 1
    coverage = result["evaluation"]["coverage"]
    assert coverage["pending_cards"] == 0 and coverage["structure_complete"] and not coverage["review_gaps"]
    assert not coverage["pipeline_complete"]  # The whole pipeline also includes Memory.
    assert result["evaluation"]["memory"]["status"] == "partial"
    assert result["evaluation"]["memory"]["coverage"]["issues"] == result["error_details"]


def test_resume_upgrades_old_character_identity_and_preserves_semantic_settings(evaluation):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    manager.provider_factory = lambda _: judge
    options = dict(analysis_version="goal_graph_v1", run_id="r", batch_rounds=1, max_calls=1,
                   max_prompt_chars=24000, max_evidence_chars=24000)
    job = manager.start(sid, aid, options)
    stopped = wait_job(manager, sid, aid, job["id"])
    assert stopped["state"] == "budget_exhausted"
    saved = manager._load(sid, aid, job["id"])
    saved["checkpoint"]["identity"]["settings"] = identity({
        k: v for k, v in saved["settings"].items() if k not in {"max_calls", "max_tokens", "max_seconds"}
    })
    saved["checkpoint"].pop("resource_limits", None)
    manager._save(saved)
    with pytest.raises(ServiceError, match="only resource limits"):
        manager.start(sid, aid, {**options, "resume_id": job["id"], "max_calls": 10, "batch_rounds": 2, "max_prompt_chars": 262144})
    with pytest.raises(ServiceError, match="cannot reduce resource"):
        manager.start(sid, aid, {**options, "resume_id": job["id"], "max_calls": 10, "max_prompt_chars": 20000})
    resumed = manager.start(sid, aid, {**options, "resume_id": job["id"], "max_calls": 10,
                                     "max_prompt_chars": 262144, "max_evidence_chars": 262144})
    result = wait_job(manager, sid, aid, resumed["id"])
    assert result["state"] == "completed", result.get("error")
    assert result["id"] == job["id"]
    assert len(judge.requests) == 2
    assert manager._load(sid, aid, job["id"])["checkpoint"]["resource_policy_upgrades"]


def test_memory_service_budget_resume_persistence_and_option_validation(evaluation):  # noqa: F811
    import json

    from loom.core import ok
    from loom.llm import LlmResponse, TokenUsage

    store, trajectory, manager, _, sid, aid = evaluation

    class MemoryJudge(GraphJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            payload = json.loads(messages[1].content)
            if payload.get('mode') not in {'extract', 'synthesis'}:
                return await super().chat(messages, tools, cancellation, **kwargs)
            rows = {k: [] for k in ('contributions', 'facts', 'lessons', 'workflows')}
            if payload['mode'] == 'extract':
                rows['contributions'] = [dict(stage_id=s['id'], goal_id=s['goal_id'], rating='unknown',
                    rationale='Only intent was recorded; no useful result established', key_action_ids=[], citations=[]) for s in payload['stages']]
            return ok(LlmResponse(content=json.dumps(rows), usage=TokenUsage(10, 5, 15)))

    judge = MemoryJudge()
    manager.provider_factory = lambda _: judge
    assert manager.options()['supports_memory']
    assert manager.options()['memory_default']
    for invalid in ('true', 1, None):
        with pytest.raises(ServiceError, match='boolean'):
            manager.start(sid, aid, dict(analysis_version='goal_graph_v1', run_id='r', extract_memory=invalid))
    with pytest.raises(ServiceError, match='incremental graph'):
        manager.start(sid, aid, dict(analysis_version='v3', extract_memory=True))
    cursor = store.snapshot(sid)['event_cursor']
    options = dict(analysis_version='goal_graph_v1', run_id='r', extract_memory=True, max_calls=2)
    job = manager.start(sid, aid, options)
    stopped = wait_job(manager, sid, aid, job['id'])
    assert stopped['state'] == 'budget_exhausted', stopped.get('error')
    assert stopped['evaluation']['memory']['status'] == 'partial'
    with pytest.raises(ServiceError, match='same model'):
        manager.start(sid, aid, dict(resume_id=job['id'], extract_memory=False, max_calls=4))
    resumed = manager.start(sid, aid, dict(resume_id=job['id'], max_calls=4))
    done = wait_job(manager, sid, aid, resumed['id'])
    assert done['state'] == 'completed', done.get('error')
    assert done['evaluation']['memory']['status'] == 'complete'
    assert done['usage']['calls'] == done['evaluation']['evaluator_usage']['calls'] == 3
    assert len(judge.requests) == 1
    assert store.snapshot(sid)['event_cursor'] == cursor
    exported = json.loads(store.artifacts.read(done['result']['sha256']))
    assert exported['memory'] == done['evaluation']['memory']
    manager.close()
    restored = SessionSemantic(trajectory, provider_factory=lambda _: judge)
    try:
        assert restored.get(sid, aid, done['id'])['evaluation']['memory'] == exported['memory']
    finally:
        restored.close()


def test_call_timeout_can_increase_on_resume_from_old_checkpoint_without_repeating_work(evaluation):  # noqa: F811
    from loom.evaluation.goal_loops.graph_contract import RESOURCE_LIMITS

    _, _, manager, _, sid, aid = evaluation
    judge = GraphJudge()
    manager.provider_factory = lambda _: judge
    started = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r", batch_rounds=1, max_calls=1))
    stopped = wait_job(manager, sid, aid, started["id"])
    assert stopped["state"] == "budget_exhausted"
    saved = manager._load(sid, aid, started["id"])
    # The previous version included the call timeout in semantic identity.
    saved["checkpoint"]["identity"]["settings"] = identity({
        k: v for k, v in saved["settings"].items() if k not in (RESOURCE_LIMITS - {"max_call_seconds"}) | {"extract_memory"}
    })
    saved["checkpoint"]["resource_limits"].pop("max_call_seconds")
    manager._save(saved)
    with pytest.raises(ServiceError, match="cannot reduce"):
        manager.start(sid, aid, dict(resume_id=started["id"], max_calls=10, max_call_seconds=100))
    resumed = manager.start(sid, aid, dict(resume_id=started["id"], max_calls=10, max_call_seconds=600))
    assert resumed["id"] == started["id"]
    done = wait_job(manager, sid, aid, resumed["id"])
    assert done["state"] == "completed", done.get("error")
    assert done["settings"]["max_call_seconds"] == 600
    assert len(judge.requests) == 2
    assert done["evaluation"]["graph_revision"] > stopped["evaluation"]["graph_revision"]


def test_service_exposes_timeout_reason_limit_and_failed_call_details(evaluation):  # noqa: F811
    from loom.llm.api import OpenAIProvider

    _, _, manager, _, sid, aid = evaluation

    async def transport(*args):
        raise TimeoutError()

    manager.provider_factory = lambda _: OpenAIProvider(api_key="fixture", model="glm-5.3", http_client=transport)
    started = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r", max_call_seconds=180))
    failed = wait_job(manager, sid, aid, started["id"])
    assert failed["state"] == "failed"
    assert failed["error_details"][0]["code"] == "request.timeout"
    assert failed["error_details"][0]["details"]["timeout_seconds"] == 180
    assert "180s" in failed["error"]
    assert "Resume" in failed["error"]
    assert "Evaluator request timed out" in failed["current_call"]["error"]
    assert "180s" in failed["current_call"]["error"]
    assert failed["current_call"]["error_code"] == "request.timeout"
    assert failed["call_history"][-1] == failed["current_call"]
    assert failed["current_call"]["duration_seconds"] >= 0
    assert any("180s" in event["message"] for event in failed["progress"] if event["kind"] == "call.failed")
    assert failed["usage"]["unreported_calls"] == 1


def test_total_time_budget_is_not_reported_as_provider_failure(evaluation):  # noqa: F811
    _, _, manager, _, sid, aid = evaluation
    judge = GraphJudge()

    async def chat(*args, **kwargs):
        await asyncio.sleep(10)

    judge.chat = chat
    manager.provider_factory = lambda _: judge
    started = manager.start(sid, aid, dict(analysis_version="goal_graph_v1", run_id="r", max_seconds=1))
    stopped = wait_job(manager, sid, aid, started["id"])
    assert stopped["state"] == "budget_exhausted"
    assert stopped["current_call"]["error_code"] == "budget.seconds"
    assert "time budget reached (1s)" in stopped["current_call"]["error"]
    assert stopped["call_history"][-1] == stopped["current_call"]
