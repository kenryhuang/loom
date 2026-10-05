import os
import signal
import time

import pytest

from loom.core import Observation
from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.service.fakes import provider_factory
from tests.service.test_controller import command, create, wait_state


def test_workspace_serialization_other_workspace_runs_concurrently(tmp_path):
    service = LoomService(tmp_path / "data", max_active_runs=2, provider_factory=provider_factory).start()
    try:
        one = create(service, tmp_path / "shared", "slow")
        wait_state(service, one, "running")
        two = create(service, tmp_path / "shared", "slow")
        three = create(service, tmp_path / "other", "question")
        wait_state(service, three, "awaiting_input")
        states = service.snapshot(two)
        assert states["task"]["state"] == "queued"
        wait_state(service, one, "idle")
        wait_state(service, two, "idle")
    finally:
        service.close()


def test_epoch_fencing_and_uncertain_tool_require_recovery(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory)
    sid = create(service, tmp_path / "workspace")
    service.prepare_attempt(sid)
    service.store.update(sid, lambda state, emit, db: state.update(input_cursor=state["messages"][0]["seq"]))
    state = service.store.snapshot(sid)
    record = {
        "attempt_id": state["run"]["attempt_id"],
        "epoch": state["epoch"],
        "record_id": "one",
        "type": "operation_start",
        "payload": encode({"id": "write", "name": "write_file", "arguments": "{}"}),
    }
    assert service.worker_record(sid, record) is None
    assert service.worker_record(sid, record) is None
    service.recover(sid, "worker lost")
    recovering = service.snapshot(sid)
    assert recovering["task"]["state"] == "recovering"
    with pytest.raises(ServiceError):
        service.worker_record(sid, {**record, "record_id": "late"})
    with pytest.raises(ServiceError):
        command(service, sid, "supersede_input", request_id=recovering["input_request"]["id"], content="Ignore")
    command(service, sid, "answer_input", request_id=recovering["input_request"]["id"], answer='{"resolution":"stop","evidence":"Cannot verify"}')
    assert service.snapshot(sid)["run"]["state"] == "stopped"
    service.close()


def test_worker_failure_isolated(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        bad = create(service, tmp_path / "bad", "crash")
        good = create(service, tmp_path / "good")
        wait_state(service, good, "idle")
        # A lost model response can be retried by explicit resume; no side effects occurred.
        wait_state(service, bad, "failed")
        assert service.snapshot(good)["run"]["state"] == "completed"
    finally:
        service.close()


def test_lost_side_effect_never_replays_without_reconciliation(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace", "uncertain")
        pending = wait_state(service, sid, "recovering")
        assert (tmp_path / "workspace" / "marker").read_text() == "once"
        time.sleep(0.15)
        assert (tmp_path / "workspace" / "marker").read_text() == "once"
        command(
            service,
            sid,
            "answer_input",
            request_id=pending["input_request"]["id"],
            answer='{"resolution":"completed","evidence":"Verified marker contains once"}',
        )
        done = wait_state(service, sid, "idle")
        assert done["run"]["id"] == pending["run"]["id"]
        assert (tmp_path / "workspace" / "marker").read_text() == "once"
        assert any(e["type"] == "tool.reconciled" for e in service.events(sid))
        context = decode(service.store.snapshot(sid)["context"])
        assert any(o.source == "process_execute" and o.value.get("reconciled") for o in context.state.observations)
    finally:
        service.close()


def test_committed_plan_transition_restores_before_context_writeback(tmp_path):
    class CrashPlanService(LoomService):
        crashed = False

        def worker_record(self, sid, record):
            response = super().worker_record(sid, record)
            if record["type"] == "event" and decode(record["payload"])["type"] == "plan.submitted" and not self.crashed:
                self.crashed = True
                os.kill(self.active[sid].process.pid, signal.SIGKILL)
            return response

    service = CrashPlanService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("plan", {"objective": "planned maintenance", "workspace": str(tmp_path), "plan_mode": "force"})["session_id"]
        failed = wait_state(service, sid, "failed", timeout=5)
        original_item = failed["plan"]["items"][0]["id"]
        command(service, sid, "resume")
        finished = wait_state(service, sid, "idle")
        assert finished["plan"]["items"][0]["id"] == original_item
        assert finished["plan"]["items"][0]["status"] == "completed"
        assert sum(e["type"] == "plan.submitted" for e in service.events(sid, limit=1000)) == 1
    finally:
        service.close()


def test_repeat_pause_cannot_resume_before_checkpoint_and_stop_has_priority(tmp_path):
    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        service.prepare_attempt(sid)
        command(service, sid, "pause")
        command(service, sid, "pause")
        assert service.snapshot(sid)["task"]["state"] == "pausing"
        with pytest.raises(ServiceError):
            command(service, sid, "resume")
        command(service, sid, "stop_run")
        command(service, sid, "pause")
        assert service.store.snapshot(sid)["control"]["kind"] == "stopped"
    finally:
        service.close()


def test_controls_cannot_bypass_uncertain_side_effect_recovery(tmp_path):
    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        state = service.prepare_attempt(sid)
        service.store.update(sid, lambda s, emit, db: s.update(input_cursor=s["messages"][0]["seq"]))
        record = {
            "attempt_id": state["run"]["attempt_id"],
            "epoch": state["epoch"],
            "record_id": "begin",
            "type": "operation_start",
            "payload": encode({"id": "effect", "name": "write_file", "arguments": "{}"}),
        }
        service.worker_record(sid, record)
        service.recover(sid, "lost")
        for kind in ("stop_run", "pause", "resume", "complete_task"):
            with pytest.raises(ServiceError):
                command(service, sid, kind)
        assert service.snapshot(sid)["input_request"]["state"] == "pending"
    finally:
        service.close()


def test_stop_merges_journal_results_not_yet_in_checkpoint(tmp_path):
    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        state = service.prepare_attempt(sid)
        service.store.update(sid, lambda s, emit, db: s.update(input_cursor=s["messages"][0]["seq"]))
        context = make_task_context(TaskRequest("Maintain", workspace=tmp_path), plan_mode="off").unwrap()
        ref = service.store.save_artifact(sid, encode({"context": context, "observations": []}), "checkpoint")
        service.store.update(sid, lambda s, emit, db: s["run"].update(checkpoint=ref))
        call = {"id": "read", "name": "read_file", "arguments": "{}"}
        record = {"attempt_id": state["run"]["attempt_id"], "epoch": state["epoch"], "record_id": "begin", "type": "operation_start", "payload": encode(call)}
        service.worker_record(sid, record)
        observation = Observation("committed", "read_file", {"content": "evidence"}, "now")
        service.worker_record(
            sid, {**record, "record_id": "finish", "type": "operation_finish", "payload": encode({"call": call, "result": {"ok": True, "value": observation}})}
        )
        service.recover(sid, "lost before checkpoint")
        command(service, sid, "stop_run")
        preserved = decode(service.store.snapshot(sid)["context"])
        assert preserved.state.observations == (observation,)
    finally:
        service.close()


def test_unconfirmed_process_cleanup_blocks_other_session_and_resumption(tmp_path, monkeypatch):
    from loom.service.scheduler import ready_sessions

    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "shared")
        other = create(service, tmp_path / "shared")
        separate = create(service, tmp_path / "other")
        service.prepare_attempt(sid)
        service.store.update(sid, lambda s, emit, db: s["run"].update(process={"pid": 123, "identity": "saved"}))
        monkeypatch.setattr("loom.service.controller.reap_group", lambda *_: False)
        service.recover(sid, "lost")
        assert list(ready_sessions(service.store.list_sessions(), {}, 2)) == [separate]
        with pytest.raises(ServiceError):
            command(service, sid, "resume")
        with pytest.raises(ServiceError):
            service.prepare_attempt(sid)
        assert service.snapshot(other)["task"]["state"] == "queued"
    finally:
        service.close()


def test_running_event_updates_frontend_without_snapshot_reload(tmp_path):
    from loom.client.projection import SessionProjection

    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        projection = SessionProjection(service.snapshot(sid))
        service.prepare_attempt(sid)
        for event in service.events(sid, projection.cursor):
            projection.apply(event)
        assert projection.snapshot["task"]["state"] == "running"
        assert projection.snapshot["task"]["revision"] == service.snapshot(sid)["task"]["revision"]
    finally:
        service.close()


def test_pause_after_terminal_checkpoint_wins_over_result_record(tmp_path):
    import asyncio

    from tests.llm.test_managed_step import Execution, Provider, final, run_managed

    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        state = service.prepare_attempt(sid)
        execution = Execution()
        result = asyncio.run(run_managed(tmp_path, Provider([final()]), execution)).unwrap()
        ref = service.store.save_artifact(sid, encode(execution.checkpoint), "checkpoint")
        service.store.update(sid, lambda s, emit, db: s["run"].update(checkpoint=ref))
        command(service, sid, "pause")
        service.worker_record(
            sid, {"attempt_id": state["run"]["attempt_id"], "epoch": state["epoch"], "record_id": "result", "type": "result", "payload": encode(result)}
        )
        assert service.snapshot(sid)["task"]["state"] == "paused"
        assert service.snapshot(sid)["run"]["state"] == "suspended"
        assert not any(m["role"] == "assistant" for m in service.snapshot(sid)["messages"])
        assert decode(service.store.snapshot(sid)["context"]).state.decisions == ()
    finally:
        service.close()


def test_terminal_worker_cannot_release_workspace_without_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    service = LoomService(tmp_path / "data", provider_factory=provider_factory)
    try:
        sid = create(service, tmp_path / "shared")
        other = create(service, tmp_path / "shared")
        state = service.prepare_attempt(sid)
        service.store.update(sid, lambda s, emit, db: s["run"].update(state="completed", process={"pid": 123, "identity": "saved"}))
        monkeypatch.setattr("loom.service.controller.reap_group", lambda *_: False)
        service.active[sid] = SimpleNamespace(
            process=SimpleNamespace(is_alive=lambda: False, join=lambda: None),
            connection=SimpleNamespace(poll=lambda: False, close=lambda: None),
            workspace=state["task"]["workspace"],
        )
        service.start()
        deadline = time.monotonic() + 1
        while sid in service.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.snapshot(other)["task"]["state"] == "queued"
        assert service.store.snapshot(sid)["workspace_blocked"]
    finally:
        service.close()
