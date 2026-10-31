"""Acceptance through the durable worker, model counters and operation journal."""

import json
import sys

from loom.service.controller import LoomService
from tests.service.fakes import FakeProvider
from tests.service.test_controller import command, wait_state
from tests.tasks.test_acceptance import Judge


def feedback_provider(state):
    from tests.tasks.test_acceptance_feedback import EvidenceJudge

    return EvidenceJudge(finish=state['task']['objective'] != 'Direct answer')


def test_durable_task_loop_repairs_missing_evidence_without_resume(tmp_path):
    (tmp_path / 'source.txt').write_text('verifiable source fact')
    service = LoomService(tmp_path / 'data', provider_factory=feedback_provider).start()
    try:
        for objective in ('Finish tool', 'Direct answer'):
            sid = service.create(objective, {'objective': objective, 'workspace': str(tmp_path), 'plan_mode': 'off'})['session_id']
            state = wait_state(service, sid, 'idle')
            assert state['acceptance']['state'] == 'passed'
            assert state['acceptance']['feedback']['state'] == 'resolved'
            assert state['acceptance']['budget']['repair_rounds'] == 1
            events = service.events(sid, limit=1000)
            assert sum(e['type'] == 'acceptance.feedback.applied' for e in events) == 1
            assert sum(e['type'] == 'acceptance.feedback.resolved' for e in events) == 1
            assert sum(e['type'] == 'run.started' for e in events) == 1
            assert sum(e['type'] == 'tool.completed' and e['payload'].get('tool_id') == 'read_file' for e in events) == 1
    finally:
        service.close()


def evidence_resume_provider(state):
    from pathlib import Path
    from loom.core import err, make_loom_error, ok
    from loom.llm import LlmResponse, LlmToolCall

    class Provider:
        model = 'evidence-resume-fixture'
        verification_provider = Judge([{'id': 'source-read', 'description': 'Read the source file',
            'verifier': 'tool_call', 'scope': ['source.txt'], 'check': {'tool_id': 'read_file'}}])

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if (Path(state['task']['workspace']) / 'resume-ready').exists():
                return ok(LlmResponse('', (LlmToolCall('finish', 'finish', '{"report":"Read source evidence"}'),)))
            if any(m.role == 'tool' for m in messages):
                return err(make_loom_error('FIXTURE_FAILURE', 'Fail after reading source', retryable=False))
            return ok(LlmResponse('', (LlmToolCall('read', 'read_file', '{"path":"source.txt"}'),)))

    return Provider()


def test_failed_run_resume_preserves_tool_evidence_for_same_goal(tmp_path):
    (tmp_path / 'source.txt').write_text('Observed source facts')
    service = LoomService(tmp_path / 'data', provider_factory=evidence_resume_provider).start()
    try:
        sid = service.create('evidence', {'objective': 'Read source and report', 'workspace': str(tmp_path), 'plan_mode': 'off'})['session_id']
        first = wait_state(service, sid, 'failed')
        import time
        deadline = time.monotonic() + 5
        while service.store.snapshot(sid)['run']['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.02)
        # Agent failures become terminal and get a new run on Resume, unlike
        # ordinary sessions which retain the suspended run's checkpoint.
        service.store.update(sid, lambda state, emit, db: state['run'].update(state='failed'))
        (tmp_path / 'resume-ready').touch()
        command(service, sid, 'resume')
        result = wait_state(service, sid, 'idle')
        assert result['run']['id'] != first['run']['id']
        assert result['acceptance']['state'] == 'passed'
        events = service.events(sid, limit=1000)
        assert sum(e['type'] == 'tool.completed' and e['payload'].get('tool_id') == 'read_file' for e in events) == 1
        assert sum(e['type'] == 'acceptance.plan.accepted' for e in events) == 1
    finally:
        service.close()


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
        assert state["acceptance"]["attempts"] == 1
        assert state["acceptance"]["decision"]["reason_code"] == "ACCEPTANCE_NO_PROGRESS"
        assert any(row["status"] == "failed" for row in state["acceptance"]["results"])
        assert not any(e["type"] == "run.completed" for e in service.events(sid, limit=1000))
    finally:
        service.close()


def test_explicit_resume_recovers_exhausted_acceptance_budget_across_service_restart(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'check.txt').write_text('pass')
    directory = tmp_path / 'data'
    service = LoomService(directory, provider_factory=provider_factory).start()
    try:
        sid = service.create('bounded', {'objective': 'Maintain', 'workspace': str(workspace), 'plan_mode': 'off',
            'task_spec': {'acceptance': {'max_model_calls': 1}, 'workflow': {'plugin': 'dynamic'},
                'session_environment': {'plugin': 'session', 'resources': [{'id': 'workspace', 'kind': 'directory', 'uri': str(workspace)}]},
                'tools': {'collections': ['filesystem', 'shell', 'task_control']}}})['session_id']
        first = wait_state(service, sid, 'paused')
        assert first['acceptance']['state'] == 'blocked'
        assert 'model calls 1/1' in first['acceptance']['reason']
        original_plan = first['acceptance']['plan']
        original_usage = first['token_budget']['used']
    finally:
        service.close()
    service = LoomService(directory, provider_factory=provider_factory).start()
    try:
        command(service, sid, 'resume')
        state = wait_state(service, sid, 'idle')
        assert state['acceptance']['state'] == 'passed'
        assert state['acceptance']['plan'] == original_plan
        assert state['acceptance']['budget']['max_model_calls'] == 1
        assert state['acceptance']['budget']['generation'] == 1
        assert state['acceptance']['budget']['total_model_calls'] == 2
        assert state['token_budget']['used'] > original_usage
        events = service.events(sid, limit=1000)
        renewed = [e for e in events if e['type'] == 'acceptance.budget.renewed']
        assert len(renewed) == 1
        archive = json.loads(service.store.read_artifact(sid, renewed[0]['payload']['previous_budget']['sha256']))
        assert archive['budget']['model_calls'] == 1
        assert not any(r['status'] == 'passed' for r in archive['results'])
    finally:
        service.close()


def test_resume_allocation_request_survives_replacing_a_failed_run(tmp_path):
    service = LoomService(tmp_path / 'data')
    try:
        sid = service.create('new', {'objective': 'Maintain', 'plan_mode': 'off'})['session_id']
        first = service.prepare_attempt(sid)

        def failed(state, emit, db):
            state['task']['state'] = 'failed'
            state['run']['state'] = 'failed'

        service.store.update(sid, failed)
        receipt = command(service, sid, 'resume')
        second = service.prepare_attempt(sid)
        assert first['run']['id'] != second['run']['id']
        assert second['run']['acceptance_budget_request'] == receipt['command_id']
    finally:
        service.close()
