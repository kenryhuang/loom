import asyncio
import json
import threading
import time

import pytest

from loom.core import ok
from loom.evaluation.diagnostics import DIMENSIONS
from loom.llm import LlmResponse, TokenUsage
from loom.service.contracts import ServiceError
from loom.service.semantic import SessionSemantic
from loom.service.store import SessionStore
from loom.service.trajectory import SessionTrajectory


class Judge:
    model = "fixture-judge"

    def __init__(self):
        self.calls = 0
        self.block_after = None
        self.blocked = threading.Event()

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        if self.block_after is not None and self.calls > self.block_after:
            self.blocked.set()
            await asyncio.sleep(60)
        payload = json.loads(messages[1].content)
        refs = [item["returned_ref"] for item in payload.get("initial_evidence", [])]
        result = {"diagnoses": [], "verification": [], "preserved_behaviors": [], "verification_framework": [], "round_analyses": []}
        for item in payload.get("trajectory", []):
            ref = next((r for r in refs if r["line_number"] == item["response_ref"]["line_number"]), None)
            if not ref:
                continue
            result["diagnoses"].append({"dimension": "loop_progress", "scope": item["id"],
                "observation": "Recorded response repeats planning", "interpretation": "No new work is established",
                "epistemic_status": "inferred", "supporting_refs": [ref], "counterevidence_refs": [],
                "mechanism": "Repeated plan", "improvement_hypothesis": "Attempt the next check before repeating planning",
                "preserve": "Keep explicit evidence", "consequence": "Additional model cost"})
            result["round_analyses"].append({"round_id": item["id"], "pre_state": "Plan available", "intent": "Plan again",
                "action": "Respond", "observed_change": "No new evidence", "post_state": "Plan available", "progress_kind": "stalled",
                "evidence_refs": [ref], "dimensions": {key: {"status": "ineffective", "rationale": "No new evidence",
                "evidence_refs": [ref]} for key in DIMENSIONS}})
        return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(10, 5, 15)))


def wait_job(manager, sid, aid, jid):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        job = manager.get(sid, aid, jid)
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(.02)
    pytest.fail("Evaluation did not finish")


@pytest.fixture
def evaluation(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = store.create("test", {"objective": "Inspect", "workspace": str(tmp_path)})["session_id"]
    def record(state, emit, db):
        state["run"] = {"id": "r", "state": "running"}
        emit("run.started", {})
        for i in range(2):
            scope = {"loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": f"c{i}"}
            emit("llm.requested", {**scope, "messages": [{"role": "user", "content": "Inspect"}], "tools": []})
            emit("llm.completed", {**scope, "response": {"content": "Plan next check",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}})
    store.update(sid, record)
    trajectory = SessionTrajectory(store)
    aid = trajectory.start(sid)["id"]
    until = time.monotonic() + 10
    while trajectory.get(sid, aid)["state"] != "completed":
        assert time.monotonic() < until
        time.sleep(.01)
    provider = Judge()
    semantic = SessionSemantic(trajectory, provider_factory=lambda model: provider)
    yield store, trajectory, semantic, provider, sid, aid
    semantic.close()
    trajectory.close()


def test_semantic_runs_real_judge_pipeline_persists_and_keeps_solver_untouched(evaluation, tmp_path):
    store, trajectory, semantic, provider, sid, aid = evaluation
    cursor = store.snapshot(sid)["event_cursor"]
    job = semantic.start(sid, aid, {})
    result = wait_job(semantic, sid, aid, job["id"])
    assert result["state"] == "completed", result
    assert provider.calls == 1
    assert result["usage"]["total_tokens"] == 15
    assert result["evaluation"]["insights"]["reviewed_rounds"] == 2
    assert result["evaluation"]["insights"]["progress_costs"]["stalled"]["known_tokens"] == 30
    assert result["evaluation"]["proposals"][0]["state"] == "proposed"
    assert result["evaluation"]["proposals"][0]["validation_status"] == "not_run"
    assert result["evaluation"]["semantic"]["round_analyses"][0]["dimensions"]["context_effectiveness"]["status"] == "unknown"
    assert semantic.start(sid, aid, {})["id"] == job["id"]
    assert store.snapshot(sid)["event_cursor"] == cursor
    other = store.create("other", {"objective": "Other", "workspace": str(tmp_path)})["session_id"]
    with pytest.raises(ServiceError):
        semantic.get(other, aid, job["id"])
    for options in ({"max_calls": True}, {"model": "unknown"}, {"trace_path": "/tmp/file"}, {"max_tokens": -1}):
        with pytest.raises(ServiceError):
            semantic.start(sid, aid, options)
    semantic.close()
    restored = SessionSemantic(trajectory, provider_factory=lambda model: provider)
    try:
        assert restored.get(sid, aid, job["id"])["evaluation"] == result["evaluation"]
        assert restored.start(sid, aid, {})["id"] == job["id"]
    finally:
        restored.close()


def test_cancel_resume_reuses_completed_batches(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    provider.block_after = 1
    job = semantic.start(sid, aid, {"batch_rounds": 1})
    assert provider.blocked.wait(5)
    semantic.cancel(sid, aid, job["id"])
    stopped = wait_job(semantic, sid, aid, job["id"])
    assert stopped["state"] == "cancelled", stopped
    assert stopped["completed_batches"] == 1
    provider.block_after = None
    assert semantic.start(sid, aid, {"batch_rounds": 1})["id"] == job["id"]
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["state"] == "completed", done
    assert provider.calls == 4  # first batch, cancelled second, resumed second, synthesis
    assert done["usage"]["unreported_calls"] == 1
    assert done["evaluation"]["insights"]["reviewed_rounds"] == 2


def test_call_budget_stops_before_another_provider_call(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    job = semantic.start(sid, aid, {"batch_rounds": 1, "max_calls": 1})
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["state"] == "budget_exhausted"
    assert "budget" in done["error"].lower()
    assert done["completed_batches"] == provider.calls == 1
    assert done["evaluation"]["semantic"]["coverage"]["status"] == "incomplete"
    assert done["evaluation"]["insights"]["reviewed_rounds"] == 1


def test_insights_keep_unknown_costs_and_revision_scopes_explicit():
    from loom.service.insights import build_insights

    summary = {"rounds": [{"id": "one", "run_id": "r", "seq": 2, "usage": {"total_tokens": None}},
                          {"id": "two", "run_id": "r", "seq": 3, "usage": {"total_tokens": 20}},
                          {"id": "three", "run_id": "other", "seq": 5, "usage": {"total_tokens": 30}}],
               "failures": [{"seq": 1, "run_id": "r", "type": "run.failed", "ref": {"line_number": 1}}],
               "task_contracts": [{"id": "old", "criteria": [{"id": "old-c"}]},
                                  {"id": "new", "supersedes": "old", "criteria": [{"id": "new-c"}]}]}
    semantic = {"round_analyses": [{"round_id": "one", "progress_kind": "stalled"},
                                  {"round_id": "two", "progress_kind": "blocker_resolution"}],
                "verification": [{"criterion_id": "old-c", "status": "supported"},
                                 {"criterion_id": "new-c", "status": "unverified"}]}
    result = build_insights(summary, semantic)
    assert result["progress_costs"]["stalled"]["unmeasured_rounds"] == 1
    assert result["progress_costs"]["unknown"]["known_tokens"] == 30
    assert result["recovery_windows"][0]["known_tokens"] == 20
    assert result["recovery_windows"][0]["status"] == "candidate_resolution"
    assert result["verification_statuses"] == {"unverified": 1}
    assert result["criteria"][0]["superseded"]
    assert result["task_completion"] == "unverified"


def test_prefix_evaluation_never_claims_full_source_review(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    job = semantic.start(sid, aid, {"max_rounds": 1})
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["state"] == "completed", done
    assert provider.calls == 1
    result = done["evaluation"]
    assert result["semantic"]["coverage"]["scope"]["selected_rounds"] == 1
    assert result["semantic"]["coverage"]["status"] == "incomplete"
    assert result["insights"]["reviewed_rounds"] == 1
    assert result["insights"]["total_rounds"] == 2
    assert result["insights"]["progress_costs"]["unknown"]["known_tokens"] == 15


def test_increasing_total_budget_resumes_same_job_without_repeating_saved_batches(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    job = semantic.start(sid, aid, {"batch_rounds": 1, "max_calls": 1})
    stopped = wait_job(semantic, sid, aid, job["id"])
    assert stopped["state"] == "budget_exhausted"
    with pytest.raises(ServiceError, match="Increase total limits"):
        semantic.start(sid, aid, {"resume_id": job["id"]})
    assert provider.calls == 1
    with pytest.raises(ServiceError, match="analysis settings"):
        semantic.start(sid, aid, {"resume_id": job["id"], "max_calls": 10, "batch_rounds": 2})
    resumed = semantic.start(sid, aid, {"resume_id": job["id"], "max_calls": 10})
    assert resumed["id"] == job["id"]
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["state"] == "completed", done
    assert provider.calls == 3  # first batch, second batch, synthesis; no repeated first batch
    assert done["usage"]["calls"] == 3


def test_exhausted_time_is_rejected_before_any_new_model_call(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    job = semantic.start(sid, aid, {"max_calls": 1, "batch_rounds": 1})
    wait_job(semantic, sid, aid, job["id"])
    body = semantic._load(sid, aid, job["id"])
    body["elapsed_seconds"] = 1801
    body["error"] = "Evaluation time budget reached; completed batches retained"
    semantic._save(body)
    with pytest.raises(ServiceError, match="max_seconds > 1801"):
        semantic.start(sid, aid, {"resume_id": job["id"], "max_calls": 10})
    assert provider.calls == 1
    semantic.start(sid, aid, {"resume_id": job["id"], "max_calls": 10, "max_seconds": 3600})
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["state"] == "completed"
    assert provider.calls == 3


def test_progress_is_durable_and_heartbeats_while_evaluator_waits(evaluation):
    _, _, semantic, provider, sid, aid = evaluation
    provider.block_after = 1
    job = semantic.start(sid, aid, {"batch_rounds": 1})
    assert provider.blocked.wait(5)
    before = semantic.get(sid, aid, job["id"])
    assert before["state"] == "running"
    assert before["completed_batches"] == 1 and before["reviewed_rounds"] == 1
    assert before["selected_rounds"] == 2
    assert before["current_call"]["state"] == "waiting"
    assert {row["kind"] for row in before["progress"]} >= {"started", "call.started", "call.completed", "batch.saved"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = semantic.get(sid, aid, job["id"])
        if current["elapsed_seconds"] > before["elapsed_seconds"] + .5:
            break
        time.sleep(.05)
    assert current["elapsed_seconds"] > before["elapsed_seconds"] + .5
    assert current["reviewed_rounds"] == 1  # No invented progress while the model waits.
    semantic.cancel(sid, aid, job["id"])
    done = wait_job(semantic, sid, aid, job["id"])
    assert done["progress"][-1]["kind"] == "cancelled"
    assert done["current_call"]["state"] == "interrupted"
