import time
import uuid

import pytest

from loom.service.controller import LoomService
from tests.service.fakes import provider_factory


def wait_state(service, sid, wanted, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.snapshot(sid)
        if state["task"]["state"] in {wanted}:
            return state
        time.sleep(0.03)
    pytest.fail(f"Expected {wanted}: {state}")


def command(service, sid, kind, **payload):
    return service.submit(sid, {"command_id": uuid.uuid4().hex, "type": kind, "payload": payload})


def create(service, path, objective="Maintain"):
    path.mkdir(exist_ok=True)
    return service.create(uuid.uuid4().hex, {"objective": objective, "workspace": str(path), "plan_mode": "off"})["session_id"]


def test_continuous_task_has_multiple_runs_and_persistent_history(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace")
        first = wait_state(service, sid, "idle")
        assert first["messages"][0]["state"] == "applied"
        command(service, sid, "submit_message", content="Fix the regression next")
        second = wait_state(service, sid, "idle")
        assert second["run"]["id"] != first["run"]["id"]
        assert second["task"]["id"] == first["task"]["id"]
        assert len([m for m in second["messages"] if m["role"] == "assistant"]) == 2
        command(service, sid, "complete_task")
        command(service, sid, "reopen_task")
        assert service.snapshot(sid)["task"]["state"] == "idle"
    finally:
        service.close()
    restarted = LoomService(tmp_path / "data", provider_factory=provider_factory)
    assert restarted.snapshot(sid)["event_cursor"] >= second["event_cursor"]
    restarted.close()


def test_human_input_survives_restart_same_run_no_repeat(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    sid = create(service, tmp_path / "workspace", "question")
    pending = wait_state(service, sid, "awaiting_input")
    service.close()
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        command(service, sid, "answer_input", request_id=pending["input_request"]["id"], answer="Payments")
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == pending["run"]["id"]
        events = service.events(sid)
        assert sum(e["type"] == "input.requested" for e in events) == 1
        assert done["input_request"]["state"] == "answered"
    finally:
        service.close()


def test_pause_resume_and_stop_preserve_partial_context(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace", "slow")
        wait_state(service, sid, "running")
        command(service, sid, "pause")
        paused = wait_state(service, sid, "paused")
        command(service, sid, "resume")
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == paused["run"]["id"]
        command(service, sid, "submit_message", content="Do another slow run")
        wait_state(service, sid, "running")
        command(service, sid, "stop_run")
        stopped = wait_state(service, sid, "paused")
        assert stopped["run"]["state"] == "stopped"
        command(service, sid, "resume")
        restarted = wait_state(service, sid, "idle")
        assert restarted["run"]["id"] != stopped["run"]["id"]
    finally:
        service.close()


def test_stopping_a_question_allows_a_new_run(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace", "question")
        pending = wait_state(service, sid, "awaiting_input")
        command(service, sid, "stop_run")
        assert service.snapshot(sid)["input_request"]["state"] == "cancelled"
        command(service, sid, "resume")
        resumed = wait_state(service, sid, "awaiting_input")
        assert resumed["run"]["id"] != pending["run"]["id"]
    finally:
        service.close()


def test_pause_can_interrupt_active_model_request(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace", "slow-model")
        wait_state(service, sid, "running")
        deadline = time.monotonic() + 3
        while not any(e["type"] == "llm.requested" for e in service.events(sid)):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        command(service, sid, "pause")
        paused = wait_state(service, sid, "paused", timeout=2)
        assert paused["run"]["state"] == "suspended"
    finally:
        service.close()


def test_active_time_budget_does_not_wait_for_model_response(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("budget", {"objective": "slow-model", "workspace": str(tmp_path), "plan_mode": "off", "limits": {"max_duration_seconds": 1}})[
            "session_id"
        ]
        paused = wait_state(service, sid, "paused", timeout=3)
        assert paused["run"]["active_seconds"] >= 1
    finally:
        service.close()


def test_next_completed_run_selects_workflow_again_and_revises_goal(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("auto", {"objective": "Maintain", "workspace": str(tmp_path)})["session_id"]
        first = wait_state(service, sid, "idle")
        command(service, sid, "submit_message", content="Now investigate a new regression")
        second = wait_state(service, sid, "idle")
        assert "new regression" in second["task"]["objective"]
        routes = [e for e in service.events(sid, limit=1000) if e["type"] == "workflow.route.selected"]
        assert len(routes) == 2
        assert second["task"]["goal_revision"] > first["task"]["goal_revision"]
    finally:
        service.close()
