import os
import signal
import time

import pytest

from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
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
        assert any(o.source == "shell_execute" and o.value.get("reconciled") for o in context.state.observations)
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
