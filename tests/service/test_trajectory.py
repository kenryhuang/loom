import json
import time

import pytest

from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from loom.service.trajectory import SessionTrajectory


def session(store, path, name="first"):
    return store.create(name, {"objective": "Inspect files", "workspace": str(path)})["session_id"]


def append_trace(store, sid, *, command="never execute this"):
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
                "tool_id": "shell_execute", "tool_call_id": "trace-json-0-1", "input": {"command": command}}
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


@pytest.mark.parametrize("command", [["ls", "-la"], None])
def test_historical_non_string_shell_command_does_not_fail_session_analysis(tmp_path, command):
    store = SessionStore(tmp_path / "data")
    sid = session(store, tmp_path)
    append_trace(store, sid, command=command)
    manager = SessionTrajectory(store)
    try:
        result = finished(manager, sid, manager.start(sid)["id"])
        assert len(result["analysis"]["tools"]) == 1
        tool = result["analysis"]["tools"][0]
        assert tool["status"] == "failed"
        assert result["analysis"]["reasoning_process"]["coverage"]["classification"]["by_class"] == {"unknown": 1}
        ref = tool["input_ref"]
        evidence = manager.evidence(sid, result["id"], ref["line_number"], ref["field_path"], 0, 1000)
        assert json.loads(evidence["content"]) == {"command": command}
    finally:
        manager.close()


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
        statistics = analysis["base"]["statistics"]
        assert statistics["basic"]["events"]["raw"] == statistics["basic"]["events"]["analyzed"]
        assert statistics["tools"]["failed"] == 1
        assert statistics["models"]["total_tokens"]["known"] == 15
        assert statistics["by_run"]["run"]["models"]["calls"] == 1
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


def test_analysis_samples_first_output_per_run_and_exposes_model_performance(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = session(store, tmp_path)

    def append(state, emit, db):
        for run, first, tokens in (("a", 1, 40), ("b", 2, 60)):
            state["run"] = {"id": run, "state": "running"}
            scope = {"trace_id": "trace", "loop_id": "loop", "step_number": 0, "llm_call_id": "same"}

            def record(kind, second, scope=scope, **extra):
                emit(kind, {**scope, "at": f"2026-10-10T00:00:{second:02d}Z", **extra})

            record("llm.requested", 0, model="test")
            record("llm.content.delta", 0, delta="")
            record("llm.reasoning.delta", first, delta="first output")
            record("llm.content.delta", 3, delta="more output")
            record("llm.completed", 5, response={"content": "done", "usage": {"completion_tokens": tokens}})

    store.update(sid, append)
    manager = SessionTrajectory(store)
    try:
        data = finished(manager, sid, manager.start(sid)["id"])["analysis"]
        rounds = {r["run_id"]: r for r in data["rounds"]}
        assert (rounds["a"]["ttft_ms"], rounds["a"]["tps"]) == (1000, 10)
        assert (rounds["b"]["ttft_ms"], rounds["b"]["tps"]) == (2000, 20)
        stats = data["base"]["statistics"]
        assert stats["basic"]["events"]["raw"] - stats["basic"]["events"]["analyzed"] == 4
        assert stats["models"]["tps"]["average"] == 15
    finally:
        manager.close()


@pytest.mark.parametrize('kind', ['tool', 'llm'])
@pytest.mark.parametrize('ending,status', [('interrupted', 'unknown'), ('cancelled', 'cancelled')])
@pytest.mark.parametrize('has_start', [False, True])
def test_interrupted_and_cancelled_calls_keep_evidence_without_start(tmp_path, kind, ending, status, has_start):
    store = SessionStore(tmp_path / 'data')
    sid = session(store, tmp_path)
    initial_cursor = store.snapshot(sid)['event_cursor']

    def append(state, emit, db):
        state['run'] = {'id': 'run', 'state': 'running'}
        scope = dict(trace_id='trace', loop_id='loop', step_number=4)
        if kind == 'tool':
            scope.update(tool_id='read_file', tool_call_id='read-1')
        else:
            scope.update(llm_call_id='model-1')
        if has_start:
            emit(f'{kind}.started' if kind == 'tool' else 'llm.requested',
                 {**scope, 'at': '2026-10-31T00:00:00Z'})
        emit(f'{kind}.{ending}', {**scope, 'reason': 'Workflow phase changed', 'at': '2026-10-31T00:00:02Z'})

    store.update(sid, append)
    manager = SessionTrajectory(store)
    try:
        job = manager.start(sid)
        result = finished(manager, sid, job['id'])
        row = result['analysis']['tools' if kind == 'tool' else 'rounds'][0]
        assert row['status'] == status
        assert row['duration_ms'] == (2000 if has_start else None)
        assert row['seq'] == initial_cursor + (2 if has_start and kind == 'tool' else 1)
        _, data = manager._data(manager._load(sid, job['id']))
        fact = data['facts']['tool_uses' if kind == 'tool' else 'trajectory'][0]
        ref = fact['evidence_refs'][-1]
        evidence = manager.evidence(sid, job['id'], ref['line_number'], ref['field_path'], 0, 2000)
        assert evidence['event_type'] == f'{kind}.{ending}'
        assert 'Workflow phase changed' in evidence['content']
        if kind == 'tool':
            assert fact['raw_output_ref'] is None
            assert fact['output_excerpt'] == ''
    finally:
        manager.close()
