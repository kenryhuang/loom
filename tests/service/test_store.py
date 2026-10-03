import json

import pytest

from loom.service.contracts import ServiceError
from loom.service.store import SessionStore


def test_create_and_command_receipts_survive_restart(tmp_path):
    payload = {"objective": "Maintain module", "workspace": str(tmp_path)}
    store = SessionStore(tmp_path / "service")
    receipt = store.create("create-1", payload)
    sid = receipt["session_id"]
    assert SessionStore(tmp_path / "service").create("create-1", payload) == receipt
    command = {"command_id": "message-1", "type": "submit_message", "payload": {"content": "Fix the regression first"}}
    accepted = store.submit(sid, command)
    assert SessionStore(tmp_path / "service").submit(sid, command) == accepted
    snapshot = store.snapshot(sid)
    assert len(snapshot["messages"]) == 2
    events = store.events(sid)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert snapshot["event_cursor"] == events[-1]["seq"]
    assert store.events(sid, after=snapshot["event_cursor"]) == []


def test_conflicting_commands_and_invalid_payloads_are_rejected(tmp_path):
    store = SessionStore(tmp_path / "service")
    payload = {"objective": "Work", "workspace": str(tmp_path)}
    sid = store.create("create-1", payload)["session_id"]
    with pytest.raises(ServiceError, match="conflict"):
        store.create("create-1", {**payload, "objective": "Changed"})
    with pytest.raises(ServiceError):
        store.submit(sid, {"command_id": "bad", "type": "submit_message", "payload": {"content": ""}})
    with pytest.raises(ServiceError):
        store.create("bad", {**payload, "api_key": "must-not-be-accepted"})
    with pytest.raises(ServiceError):
        store.submit(sid, {"command_id": "bad-control", "type": "resume", "payload": {}, "expected_task_revision": True})


def test_mutations_rollback_events_and_state_together(tmp_path):
    store = SessionStore(tmp_path / "service")
    sid = store.create("c", {"objective": "Work", "workspace": str(tmp_path)})["session_id"]
    before = store.snapshot(sid)

    def fail(state, emit, _db):
        state["task"]["state"] = "running"
        emit("task.state.changed", {"state": "running"})
        raise ValueError("rollback")

    with pytest.raises(ValueError):
        store.update(sid, fail)
    assert store.snapshot(sid) == before


def test_artifacts_are_scoped_and_integrity_checked(tmp_path):
    store = SessionStore(tmp_path / "service")
    one = store.create("one", {"objective": "One", "workspace": str(tmp_path)})["session_id"]
    two = store.create("two", {"objective": "Two", "workspace": str(tmp_path)})["session_id"]
    ref = store.save_artifact(one, {"result": "ok"}, "log")
    assert json.loads(store.read_artifact(one, ref["sha256"])) == {"result": "ok"}
    with pytest.raises(ServiceError):
        store.read_artifact(two, ref["sha256"])
    (store.directory / ref["relative_path"]).write_text("corrupted")
    with pytest.raises(ServiceError, match="integrity"):
        store.read_artifact(one, ref["sha256"])


def test_canonical_workspace_aliases_are_equal(tmp_path):
    alias = tmp_path / "alias"
    root = tmp_path / "root"
    root.mkdir()
    alias.symlink_to(root, target_is_directory=True)
    store = SessionStore(tmp_path / "service")
    sid = store.create("c", {"objective": "Work", "workspace": str(alias)})["session_id"]
    assert store.snapshot(sid)["task"]["workspace"] == str(root.resolve())


def test_empty_session_persists_without_run_until_first_task(tmp_path):
    store = SessionStore(tmp_path / "service")
    receipt = store.create("empty", {"workspace": str(tmp_path)})
    sid = receipt["session_id"]
    store = SessionStore(tmp_path / "service")
    assert store.create("empty", receipt["input"]) == receipt
    waiting = store.snapshot(sid)
    assert waiting["task"]["state"] == "idle"
    assert waiting["task"]["objective"] == ""
    assert waiting["run"] is None
    assert waiting["messages"] == []
    first = {"command_id": "first", "type": "submit_message", "payload": {"content": "Investigate the parser"}}
    store.submit(sid, first)
    store.submit(sid, first)
    started = store.snapshot(sid)
    assert started["task"]["state"] == "queued"
    assert started["task"]["objective"] == "Investigate the parser"
    assert started["title"] == "Investigate the parser"
    assert started["task"]["id"] == waiting["task"]["id"]
    assert len(started["messages"]) == 1
    assert any(e["type"] == "task.goal.revised" for e in store.events(sid))


def test_default_token_budget_is_ten_million(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = store.create("new", {"workspace": str(tmp_path)})["session_id"]
    assert store.snapshot(sid)["task"]["limits"]["max_tokens"] == 10_000_000


def test_token_budget_update_is_durable_deduplicated_and_preserves_execution(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = store.create("new", {"objective": "Work", "workspace": str(tmp_path), "limits": {"max_tokens": 100_000}})["session_id"]

    def pause(state, emit, db):
        state["task"]["state"] = "paused"
        state["run"] = {"id": "run", "state": "suspended", "checkpoint": "saved", "counters": {"used": 120000}}

    store.update(sid, pause)
    before = store.snapshot(sid)
    command = {"command_id": "budget", "type": "set_token_budget", "payload": {"max_tokens": 20_000_000}}
    receipt = store.submit(sid, command)
    store = SessionStore(tmp_path / "data")
    assert store.submit(sid, command) == receipt
    after = store.snapshot(sid)
    assert after["task"]["limits"]["max_tokens"] == 20_000_000
    assert after["run"] == before["run"]
    assert after["task"]["state"] == "paused"
    assert after["messages"] == before["messages"]
    assert sum(event["type"] == "task.budget.changed" for event in store.events(sid)) == 1
    with pytest.raises(ServiceError, match="conflict"):
        store.submit(sid, {**command, "payload": {"max_tokens": 30_000_000}})


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10M"])
def test_budget_api_rejects_invalid_values_without_mutation(tmp_path, value):
    store = SessionStore(tmp_path / "data")
    sid = store.create("new", {"workspace": str(tmp_path)})["session_id"]
    before = store.snapshot(sid)
    with pytest.raises(ServiceError):
        store.submit(sid, {"command_id": "bad", "type": "set_token_budget", "payload": {"max_tokens": value}})
    assert store.snapshot(sid) == before


def test_budget_requires_suspended_executor_even_if_task_already_shows_paused(tmp_path):
    store = SessionStore(tmp_path / "data")
    sid = store.create("new", {"workspace": str(tmp_path)})["session_id"]
    store.update(sid, lambda state, emit, db: state.update(run={"state": "running"}))
    with pytest.raises(ServiceError, match="Pause"):
        store.submit(sid, {"command_id": "budget", "type": "set_token_budget", "payload": {"max_tokens": 20_000_000}})
