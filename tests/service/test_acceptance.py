"""Acceptance through the durable worker, model counters and operation journal."""

import json
import sys

from loom.service.controller import LoomService
from tests.service.fakes import FakeProvider
from tests.service.test_controller import wait_state
from tests.tasks.test_acceptance import Judge


def provider_factory(state):
    provider = FakeProvider(state)
    criterion = {
        "id": "regression",
        "description": "Recorded project check",
        "verifier": "command",
        "scope": ["check.txt"],
        "check": {
            "tool_id": "process_execute",
            "input": {"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('check.txt').read_text() == 'pass'"]},
        },
    }
    provider.verification_provider = Judge([criterion])
    return provider


def test_durable_command_acceptance_counts_calls_and_journals_checks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "check.txt").write_text("pass")
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("verified", {"objective": "Maintain", "workspace": str(workspace), "plan_mode": "off"})["session_id"]
        state = wait_state(service, sid, "idle")
        assert state["acceptance"]["state"] == "passed"
        assert state["token_budget"]["used"] == 30
        events = service.events(sid, limit=1000)
        assert sum(e["type"] == "llm.requested" for e in events) == 3
        with service.store.transaction() as db:
            operations = [json.loads(row[0]) for row in db.execute("SELECT body FROM operations WHERE session_id=?", (sid,))]
        checks = [op for op in operations if op["call"]["name"] == "process_execute"]
        assert len(checks) == 1 and checks[0]["status"] == "completed"
        assert checks[0]["call"]["id"].startswith("acceptance:")
        row = next(r for r in state["acceptance"]["results"] if r["criterion_id"] == "regression")
        receipt = json.loads(service.store.read_artifact(sid, row["artifact"]["sha256"]))
        assert receipt["detail"]["exit_code"] == 0
        assert receipt["binding_before"]["files"]["check.txt"]
    finally:
        service.close()


def test_durable_failed_check_suspends_and_never_commits_success(tmp_path):
    (tmp_path / "check.txt").write_text("fail")
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        sid = service.create("failed", {"objective": "Maintain", "workspace": str(tmp_path), "plan_mode": "off"})["session_id"]
        state = wait_state(service, sid, "paused")
        assert state["run"]["state"] == "suspended"
        assert state["acceptance"]["attempts"] == 3
        assert any(row["status"] == "failed" for row in state["acceptance"]["results"])
        assert not any(e["type"] == "run.completed" for e in service.events(sid, limit=1000))
    finally:
        service.close()
