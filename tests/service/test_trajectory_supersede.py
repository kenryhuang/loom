import json
import time

import pytest

from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from loom.service.trajectory import SessionTrajectory


def store_with_trace(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = store.create("test", {"objective": "Inspect", "workspace": str(tmp_path)})["session_id"]
    append_trace(store, sid, "Inspect")
    return store, sid


def append_trace(store, sid, text):
    def record(state, emit, db):
        if not state["run"]:
            state["run"] = {"id": "r", "state": "running"}
            emit("run.started", {})
        scope = {"loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": f"c{time.monotonic_ns()}"}
        emit("llm.requested", {**scope, "messages": [{"role": "user", "content": text}], "tools": []})
        emit("llm.completed", {**scope, "response": {"content": "Done",
                                                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}})

    store.update(sid, record)


def completed(trajectory, store, sid):
    # Analyses dedup on (session, cursor, version): new events advance the cursor.
    append_trace(store, sid, "More")
    aid = trajectory.start(sid)["id"]
    until = time.monotonic() + 10
    while trajectory.get(sid, aid)["state"] != "completed":
        assert time.monotonic() < until
        time.sleep(0.01)
    return aid


def test_new_analysis_supersedes_prior_completed(tmp_path):
    store, sid = store_with_trace(tmp_path)
    trajectory = SessionTrajectory(store)
    try:
        first = completed(trajectory, store, sid)
        first_body = trajectory.get(sid, first)
        first_digests = {first_body["source"]["sha256"], first_body["result"]["sha256"]}
        for digest in first_digests:
            assert store.artifacts.location(digest).exists()

        second = completed(trajectory, store, sid)
        assert second != first
        with pytest.raises(ServiceError) as error:
            trajectory.get(sid, first)
        assert error.value.status == 404
        # File unlink runs as an after-commit callback on the writer thread; wait for it.
        until = time.monotonic() + 10
        while any(store.artifacts.location(digest).exists() for digest in first_digests):
            assert time.monotonic() < until
            time.sleep(0.01)
        with store.read() as db:
            assert not db.execute("SELECT 1 FROM artifacts WHERE digest IN (?,?)", tuple(first_digests)).fetchone()
        # The newest snapshot stays fully loadable.
        body = trajectory.get(sid, second)
        store.artifacts.read(body["source"]["sha256"])
        store.artifacts.read(body["result"]["sha256"])
    finally:
        trajectory.close()


def test_supersede_preserves_analysis_pinned_by_resumable_evaluation(tmp_path):
    store, sid = store_with_trace(tmp_path)
    trajectory = SessionTrajectory(store)
    try:
        first = completed(trajectory, store, sid)
        with store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS semantic_jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cache_key TEXT UNIQUE, body TEXT NOT NULL)")
            db.execute("INSERT INTO semantic_jobs VALUES(?,?,?,?)",
                       ("job-1", sid, "key-1", json.dumps({"id": "job-1", "session_id": sid, "analysis_id": first, "state": "running"})))

        completed(trajectory, store, sid)
        assert trajectory.get(sid, first)["id"] == first  # Pinned: a running evaluation resumes from it.

        with store.transaction() as db:
            db.execute("UPDATE semantic_jobs SET body=json_set(body,'$.state','completed') WHERE id='job-1'")
        completed(trajectory, store, sid)
        with pytest.raises(ServiceError):
            trajectory.get(sid, first)
    finally:
        trajectory.close()
