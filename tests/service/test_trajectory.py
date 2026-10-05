import json
import time

import pytest

from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from loom.service.trajectory import SessionTrajectory


def session(store, path, name="first"):
    return store.create(name, {"objective": "Inspect files", "workspace": str(path)})["session_id"]


def append_trace(store, sid):
    def append(state, emit, db):
        state["run"] = {"id": "run", "state": "running"}
        emit("run.started", {})
        emit("step.started", {"trace_id": "trace", "loop_id": "loop", "step_number": 0})
        request = {"type": "llm.requested", "trace_id": "trace", "loop_id": "loop", "llm_call_id": "trace-llm-1",
                   "model": "test-model", "messages": [{"role": "user", "content": "Original input " + "x" * 9000}], "tools": []}
        ref = store.artifact_in_transaction(db, sid, request, "event_detail")
        emit("llm.requested", {"llm_call_id": "trace-llm-1", "trace_id": "trace", "artifact": ref})
        emit("llm.content.delta", {"llm_call_id": "trace-llm-1", "delta": "ignored streaming chunk"})
        emit("llm.completed", {"trace_id": "trace", "loop_id": "loop", "llm_call_id": "trace-llm-1",
                               "response": {"content": "Read source", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}})
        tool = {"trace_id": "trace", "loop_id": "loop", "step_number": 0,
                "tool_id": "shell_execute", "tool_call_id": "trace-json-0-1", "input": {"command": "never execute this"}}
        emit("tool.started", tool)
        emit("tool.completed", {**tool, "output": {"ok": False, "exit_code": 2, "stderr": "Read failed"}})
        emit("run.failed", {"code": "WORKFLOW_ROUTE_FAILED", "message": "No route"})
    store.update(sid, append)


def finished(manager, sid, identifier):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = manager.get(sid, identifier)
        if result["state"] in {"completed", "failed"}:
            assert result["state"] == "completed", result
            return result
        time.sleep(.02)
    pytest.fail("Analysis did not finish")


def test_analysis_restores_source_links_usage_and_pins_evidence_across_updates_and_restart(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = session(store, tmp_path)
    other = session(store, tmp_path, "other")
    append_trace(store, sid)
    manager = SessionTrajectory(store)
    try:
        job = manager.start(sid)
        assert manager.start(sid)["id"] == job["id"]
        result = finished(manager, sid, job["id"])
        analysis = result["analysis"]
        assert len(analysis["rounds"]) == len(analysis["tools"]) == 1
        assert analysis["tokens"]["known_total_tokens"] == 15
        assert analysis["coverage"]["hydrated_artifacts"] == 1
        assert analysis["coverage"]["unscoped_model_events"] == 0
        assert analysis["semantic_status"] == "not_evaluated"
        assert analysis["task_completion"] == "unverified"
        assert analysis["tools"][0]["round_id"] == analysis["rounds"][0]["id"]
        assert analysis["tools"][0]["status"] == "failed"
        assert analysis["failures"][0]["message"] == "No route"
        ref = analysis["rounds"][0]["request_ref"]
        first = manager.evidence(sid, job["id"], ref["line_number"], "messages.0.content", 0, 100)
        assert first["truncated"] and first["content"].startswith("Original input")
        rest = manager.evidence(sid, job["id"], ref["line_number"], "messages.0.content", first["returned_ref"]["end"], 20000)
        assert first["content"] + rest["content"] == "Original input " + "x" * 9000
        assert manager.round(sid, job["id"], analysis["rounds"][0]["id"])["context_deltas"][0]["message_count"] == 1
        with pytest.raises(ServiceError, match="not found"):
            manager.get(other, job["id"])
        with pytest.raises(ServiceError, match="not found"):
            manager.evidence(other, job["id"], ref["line_number"], None, 0, 100)
        with pytest.raises(ServiceError):
            manager.evidence(sid, job["id"], ref["line_number"], "missing.field", 0, 100)
        with pytest.raises(ServiceError):
            manager.round(sid, job["id"], "unrelated-round")
        store.update(sid, lambda state, emit, db: emit("task.state.changed", {"state": "idle"}))
        assert manager.get(sid, job["id"])["source_cursor"] == job["source_cursor"]
        newer = manager.start(sid)
        assert newer["id"] != job["id"] and newer["source_cursor"] > job["source_cursor"]
        finished(manager, sid, newer["id"])
    finally:
        manager.close()
    manager = SessionTrajectory(store)
    try:
        assert manager.get(sid, job["id"])["analysis"]["source_sha256"] == analysis["source_sha256"]
        assert manager.start(sid)["id"] == newer["id"]
    finally:
        manager.close()


def test_empty_session_and_missing_cross_session_artifact_are_explicit(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid, other = session(store, tmp_path), session(store, tmp_path, "other")
    manager = SessionTrajectory(store)
    try:
        empty = finished(manager, sid, manager.start(sid)["id"])
        assert empty["analysis"]["rounds"] == []
        assert empty["analysis"]["task_completion"] == "unverified"
        with store.transaction() as db:
            ref = store.artifact_in_transaction(db, other, {"secret": "other-session"}, "event_detail")
        store.update(sid, lambda state, emit, db: emit("llm.requested", {"artifact": ref, "llm_call_id": "orphan"}))
        result = finished(manager, sid, manager.start(sid)["id"])
        assert result["analysis"]["coverage"]["missing_artifacts"]
        assert result["analysis"]["coverage"]["unscoped_model_events"] == 1
        assert "other-session" not in json.dumps(result)
    finally:
        manager.close()
