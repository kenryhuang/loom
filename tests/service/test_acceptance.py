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


def external_acceptance_provider(state):
    from tests.service.test_external_tools import ExternalProvider

    provider = ExternalProvider(state)
    provider.verification_provider = Judge([{
        "id": "external-used", "description": "Use the attached external tool", "verifier": "tool_call", "scope": [],
        "check": {"tool_id": state["task"]["external_tool_ids"][0]},
    }])
    return provider


def test_durable_external_call_acceptance_uses_recorded_artifact(tmp_path):
    from tests.external_tools.test_external_tools import cli_config

    service = LoomService(tmp_path / "data", provider_factory=external_acceptance_provider).start()
    try:
        connection = service.store.external_tools.save(cli_config())
        tid = connection["tools"][0]["id"]
        sid = service.create("external-verified", {"objective": "Use Echo to return hello", "external_tool_ids": [tid],
                                                  "plan_mode": "off", "task_spec": {"tools": {"collections": ["task_control"]}}})["session_id"]
        state = wait_state(service, sid, "idle")
        assert state["acceptance"]["state"] == "passed"
        result = next(r for r in state["acceptance"]["results"] if r["criterion_id"] == "external-used")
        assert result["status"] == "passed" and result["method"] == "tool_call"
        receipt = json.loads(service.store.read_artifact(sid, result["artifact"]["sha256"]))
        assert receipt["detail"]["successful"] == 1
        ref = receipt["detail"]["evidence"][0]["artifact"]
        output = json.loads(service.store.read_artifact(sid, ref["sha256"]))
        assert json.loads(output["result"]["stdout"]) == {"message": "hello"}
    finally:
        service.close()


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
