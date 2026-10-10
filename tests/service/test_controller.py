import time
import uuid

import pytest

from loom.runtime.checkpoints import encode
from loom.service.controller import LoomService
from tests.service.fakes import provider_factory, routing_failure_provider_factory


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


def resume_provider_factory(state):
    provider = provider_factory(state)
    if state["run"].get("time_budget_start_seconds", 0):
        provider.objective = "Maintain"
    return provider


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
        while not any(e["type"] == "llm.requested" and e.get("payload", {}).get("usage_role") != "verification" for e in service.events(sid)):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        command(service, sid, "pause")
        paused = wait_state(service, sid, "paused", timeout=2)
        assert paused["run"]["state"] == "suspended"
    finally:
        service.close()


def test_active_time_budget_interrupts_model_and_resume_grants_another_allowance(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=resume_provider_factory).start()
    try:
        sid = service.create("budget", {"objective": "slow-model", "workspace": str(tmp_path), "plan_mode": "off", "limits": {"max_duration_seconds": 3}})[
            "session_id"
        ]
        paused = wait_state(service, sid, "paused", timeout=5)
        assert paused["run"]["active_seconds"] >= 2.4  # Cooperative stopping reserves the final 20%.
        command(service, sid, "resume")
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == paused["run"]["id"]
        assert done["run"]["time_budget_start_seconds"] == paused["run"]["active_seconds"]
        assert done["run"]["active_seconds"] > paused["run"]["active_seconds"]
        assert done["task"]["limits"]["max_duration_seconds"] == 3
    finally:
        service.close()


@pytest.mark.parametrize("kind", ["poll_control", "check_control"])
def test_renewed_time_budget_still_interrupts_at_the_next_limit(tmp_path, kind):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory)
    try:
        sid = create(service, tmp_path / "workspace")
        state = service.prepare_attempt(sid)

        def elapsed(state, emit, db):
            state["run"].update(active_seconds=3600, time_budget_start_seconds=1800)

        service.store.update(sid, elapsed)

        def poll(record_id):
            return service.worker_record(
                sid,
                {"attempt_id": state["run"]["attempt_id"], "epoch": state["epoch"], "record_id": record_id, "type": kind, "payload": encode({})},
            )

        assert poll("before-limit") in (None, False)
        service.store.update(sid, lambda state, emit, db: state["run"].update(active_seconds=3601))
        result = poll("after-limit")
        if kind == "check_control":
            assert result is True
        else:
            assert result == {"kind": "paused", "reason": "Active time budget exceeded"}
    finally:
        service.close()


def test_increased_token_budget_resumes_same_run_without_repeating_model_request(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("tokens", {"objective": "token-budget", "workspace": str(tmp_path), "plan_mode": "off", "limits": {"max_tokens": 5}})["session_id"]
        paused = wait_state(service, sid, "paused")
        assert paused["token_budget"] == {"limit": 5, "used": 10, "remaining": 0}
        command(service, sid, "set_token_budget", max_tokens=20)
        command(service, sid, "resume")
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == paused["run"]["id"]
        assert done["token_budget"] == {"limit": 20, "used": 10, "remaining": 10}
        assert sum(e["type"] == "llm.requested" and e.get("payload", {}).get("usage_role") != "verification" for e in service.events(sid)) == 1
        usage = [e for e in service.events(sid) if e["type"] == "run.usage.changed"]
        assert len(usage) == 1
        assert usage[0]["payload"]["total_tokens"] == 10
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


def test_empty_session_waits_across_restart_then_first_message_executes(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    sid = service.create("empty", {"workspace": str(tmp_path), "plan_mode": "off"})["session_id"]
    try:
        time.sleep(0.15)
        assert service.snapshot(sid)["run"] is None
        assert service.active == {}
    finally:
        service.close()
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        assert service.snapshot(sid)["run"] is None
        command(service, sid, "submit_message", content="Maintain parser")
        done = wait_state(service, sid, "idle")
        assert done["run"]["state"] == "completed"
        assert done["messages"][0]["content"] == "Maintain parser"
        assert done["messages"][0]["state"] == "applied"
    finally:
        service.close()


@pytest.mark.parametrize("command_type", ["resume", "submit_message"])
def test_route_failure_can_retry_and_new_guidance_is_applied(tmp_path, command_type):
    service = LoomService(tmp_path / "data", provider_factory=routing_failure_provider_factory).start()
    try:
        sid = service.create("route", {"objective": "Maintain", "workspace": str(tmp_path)})["session_id"]
        failed = wait_state(service, sid, "failed")
        deadline = time.monotonic() + 3
        while sid in service.active:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert failed["run"]["failure"]["code"] == "WORKFLOW_ROUTE_FAILED"
        assert failed["token_budget"]["used"] == 6
        payload = {"content": "Run the new query sample"} if command_type == "submit_message" else {}
        receipt = command(service, sid, command_type, **payload)
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == failed["run"]["id"]
        assert "failure" not in done["run"]
        assert done["token_budget"]["used"] == 6
        events = service.events(sid, limit=1000)
        assert sum(event["type"] == "llm.requested" and event.get("payload", {}).get("usage_role") != "verification" for event in events) == 4
        if command_type == "submit_message":
            message = next(message for message in done["messages"] if message["command_id"] == receipt["command_id"])
            assert message["state"] == "applied"
            assert "Run the new query sample" in done["task"]["objective"]
    finally:
        service.close()


@pytest.mark.parametrize("guard", [None, "recovery", "blocked", "pause"])
def test_guidance_at_worker_failure_waits_for_cleanup_and_preserves_guards(tmp_path, guard):
    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        service.prepare_attempt(sid)
        service.store.update(sid, lambda state, emit, db: state["task"].update(state="failed"))
        command(service, sid, "submit_message", content="Retry with this new guidance")
        assert service.snapshot(sid)["task"]["state"] == "failed"

        def block(state, emit, db):
            if guard == "recovery":
                state["input_request"] = {"id": "verify", "kind": "recovery", "state": "pending"}
            if guard == "pause":
                state["control"] = {"kind": "paused", "reason": "User pause"}

        service.store.update(sid, block)
        if guard == "blocked":
            service._cleanup_groups = lambda state, emit, db: state.update(workspace_blocked=[{"pid": 999}])
        service.recover(sid, "Worker exited after failure")
        assert service.snapshot(sid)["task"]["state"] == {None: "queued", "recovery": "awaiting_input", "blocked": "failed", "pause": "paused"}[guard]
        assert "retry_requested" not in service.snapshot(sid)["run"]
    finally:
        service.close()


def test_budget_wrap_up_is_persisted_as_partial_assistant_answer(tmp_path):
    from tests.service.fakes import budget_wrap_provider_factory

    (tmp_path / "notes.txt").write_text("Available evidence")
    service = LoomService(tmp_path / "data", provider_factory=budget_wrap_provider_factory).start()
    try:
        sid = service.create("wrap", {"objective": "Research the notes", "workspace": str(tmp_path), "plan_mode": "off",
                                      "limits": {"max_llm_calls": 3}})["session_id"]
        state = wait_state(service, sid, "paused")
        replies = [m for m in state["messages"] if m["role"] == "assistant"]
        assert len(replies) == 1
        assert "Completed:" in replies[0]["content"] and "Remaining:" in replies[0]["content"]
        assert state["run"]["state"] == "suspended"
        events = service.events(sid, limit=1000)
        assert any(e["type"] == "run.wrapping_up" for e in events)
        llm = [e for e in events if e["type"] == "llm.requested"]
        assert len(llm) == 3
        assert llm[-1]["payload"]["tools"] == []
    finally:
        service.close()
