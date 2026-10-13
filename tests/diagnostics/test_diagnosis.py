import asyncio
import json
import time

import pytest

from loom.diagnostics.evidence import EvidenceView, capture
from loom.diagnostics.jobs import SessionDiagnosis
from loom.diagnostics.report import DiagnosisAcceptance
from loom.diagnostics.runner import registry_factory, specification
from loom.diagnostics.tools import collection
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from tests.diagnostics.fakes import factory, report


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sample.py").write_text("value = 42\n")
    service = LoomService(tmp_path / "data")
    sid = service.create("target", {"title": "Target", "workspace": str(root)})["session_id"]
    yield service, sid, root
    service.close()


def snapshot(setup, tmp_path):
    service, sid, root = setup
    directory = tmp_path / "snapshot"
    capture(service.store, sid, directory, root, {"api_key": "hidden", "model": "fake"})
    return directory


def test_snapshot_boundary_redaction_and_immutability(setup, tmp_path):
    service, sid, root = setup
    (root / ".env").write_text("SECRET=never-show")
    (root / "config.yaml").write_text("api_key: never-show")
    (root / "escape.py").symlink_to(root / "config.yaml")
    (root / "sample.py").write_text("value = 42\n# Bearer sensitive-bearer\n")
    other = service.create("other", {"workspace": str(root)})["session_id"]
    with service.store.transaction() as db:
        ref = service.store.artifact_in_transaction(db, other, {"value": "cross-session-secret"}, "test")
    service.store.update(sid, lambda state, emit, db: emit("test", {"artifact": ref, "api_key": "never-show"}))
    directory = snapshot(setup, tmp_path)
    view = EvidenceView(directory)
    paths = view.query("SELECT path FROM source")
    assert paths == [("sample.py",)]
    assert "sensitive-bearer" not in view.query("SELECT body FROM source")[0][0]
    assert "never-show" not in view.query("SELECT body FROM events ORDER BY seq DESC LIMIT 1")[0][0]
    assert ref["sha256"] in view.manifest["missing_artifacts"]
    with pytest.raises(ValueError):
        view.read("artifact:" + ref["sha256"])
    (root / "sample.py").write_text("changed = True")
    assert "value = 42" in view.query("SELECT body FROM source")[0][0]


def test_private_collection_only_exposes_snapshot_readers_and_preserves_evidence(setup, tmp_path):
    service, sid, root = setup
    directory = snapshot(setup, tmp_path)
    request = TaskRequest(objective="Diagnose", workspace=root, task_spec=specification(root, directory))
    assembly = TaskAssembly(request, registry=registry_factory())
    assert not {"write_file", "edit_file", "shell_execute", "process_execute", "fetch_url"} & assembly.bindings.keys()
    assert all(binding.effect_kind in {"read_only", "service_control"} for binding in assembly.bindings.values())
    tools = collection(request)
    overview = asyncio.run(tools.entrypoints["session_diagnostics/diagnosis_overview"]({}))
    assert overview.ok
    ref = overview.value.value["id"]
    for _ in range(10):
        assert asyncio.run(tools.entrypoints["session_diagnostics/read_source_file"]({"path": "sample.py"})).ok
    denied = asyncio.run(tools.entrypoints["session_diagnostics/read_source_file"]({"path": "../config.yaml"}))
    assert not denied.ok
    policy = DiagnosisAcceptance(directory)
    assert policy.evidence(json.dumps(report(ref)))[0]["id"] == ref
    broken = report("invented@0:1")
    with pytest.raises(ValueError, match="actually read"):
        policy.validate(json.dumps(broken))
    with pytest.raises(ValueError, match="Unavailable tools plugin"):
        TaskAssembly(request)


def wait_job(manager, sid, jid):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = manager.get(sid, jid)
        if job["state"] not in {"queued", "running", "preparing"}:
            return job
        time.sleep(0.05)
    pytest.fail(str(job))


def test_persisted_real_loop_and_original_session_unchanged(setup):
    service, sid, root = setup
    original = service.store.snapshot(sid)
    manager = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        job = manager.start(sid, {"mode": "read_only", "request_id": "first"})
        assert manager.start(sid, {"mode": "read_only", "request_id": "first"})["id"] == job["id"]
        with pytest.raises(ServiceError, match="different diagnosis"):
            manager.start(sid, {"mode": "read_only", "request_id": "first", "question": "different"})
        job = wait_job(manager, sid, job["id"])
        assert job["state"] == "completed", job
        assert job["counters"]["model_calls"] >= 2
        assert job["counters"]["evidence_reads"] == 1
        assert job["counters"]["tool_calls"] >= 2
        assert job["report"]["status"] == "inconclusive"
        assert job["report_ref"]["kind"] == "diagnosis_report"
        ref = job["report"]["findings"][0]["evidence_ids"][0]
        assert manager.evidence(sid, job["id"], ref)["sanitized"]
        assert manager.events(sid, job["id"])["events"]
        assert service.store.snapshot(sid) == original
        assert len(service.store.list_sessions()) == 1
        assert (root / "sample.py").read_text() == "value = 42\n"
        other = service.create("other", {"workspace": str(root)})["session_id"]
        with pytest.raises(ServiceError, match="not found"):
            manager.get(other, job["id"])
        service.store.update(sid, lambda state, emit, db: emit("new.target.event", {}))
        assert manager.get(sid, job["id"])["stale"]
    finally:
        manager.close()
    restarted = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        assert restarted.get(sid, job["id"])["report"] == job["report"]
    finally:
        restarted.close()


def test_shared_capacity_and_cancel_queued_job(setup):
    service, sid, root = setup
    service.capacity = 1
    # A real admission reservation occupies the shared limit without running a model.
    from types import SimpleNamespace

    service.active["occupied"] = SimpleNamespace(resource_claims=[{"resource_id": "test", "access_mode": "none"}])
    manager = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        job = manager.start(sid, {"mode": "read_only", "request_id": "queued"})
        time.sleep(0.15)
        assert manager.get(sid, job["id"])["state"] == "queued"
        assert manager.cancel(sid, job["id"])["state"] == "cancelled"
        assert manager.cancel(sid, job["id"])["state"] == "cancelled"
        assert not manager.runtime.active
    finally:
        manager.close()
        service.active.clear()


def test_tool_budget_resume_reuses_snapshot_and_read_evidence(setup):
    service, sid, root = setup
    manager = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        job = manager.start(sid, {"mode": "read_only", "request_id": "budget", "budgets": {"max_tool_calls": 1}})
        job = wait_job(manager, sid, job["id"])
        assert job["state"] == "budget_exhausted", job
        assert job["counters"]["tool_calls"] == 1
        with pytest.raises(ServiceError, match="Increase"):
            manager.resume(sid, job["id"], {"budgets": {}})
        deadline = time.monotonic() + 5
        while manager.runtime.active and time.monotonic() < deadline:
            time.sleep(0.05)
        original = job["snapshot"]
        manager.resume(sid, job["id"], {"budgets": {"max_tool_calls": 10}})
        done = wait_job(manager, sid, job["id"])
        assert done["state"] == "completed", done
        assert done["snapshot"] == original
        assert done["counters"]["evidence_reads"] == 1
    finally:
        manager.close()


def test_run_filters_artifact_fields_and_cursor_are_bound(setup, tmp_path):
    service, sid, root = setup

    def event(run, marker):
        def append(state, emit, db):
            state["run"] = {"id": run}
            emit("tool.completed", {"tool_call_id": marker, "result": marker})

        service.store.update(sid, append)

    event("first", "old")
    event("second", "selected")
    with service.store.transaction() as db:
        ref = service.store.artifact_in_transaction(db, sid, {"result": {"nested": ["value"]}, "other": "large"}, "test")
    service.store.update(sid, lambda state, emit, db: emit("artifact.saved", {"artifact": ref}))
    directory = snapshot(setup, tmp_path)
    event("second", "after-snapshot")
    tools = collection(TaskRequest(objective="Review", workspace=root, task_spec=specification(root, directory)))
    call = tools.entrypoints["session_diagnostics/query_session_events"]
    result = asyncio.run(call({}))
    assert result.ok
    content = result.value.value["content"]
    assert "selected" in content and "after-snapshot" not in content and '"old"' not in content
    result = asyncio.run(call({"include_prior_context": True, "tool_call_id": "old"}))
    assert '"old"' in result.value.value["content"] and '"selected"' not in result.value.value["content"]
    view = EvidenceView(directory)
    page = view.read("artifact:" + ref["sha256"], field="/result/nested/0")
    assert page["content"] == "value"
    with pytest.raises(ValueError, match="unavailable"):
        view.read("artifact:" + ref["sha256"], field="/other/missing")
    ledger = view.ledger()
    ledger["read_chars"] = 1_000_000
    from loom.diagnostics.evidence import write_json

    write_json(directory / "ledger.json", ledger)
    with pytest.raises(ValueError, match="budget exhausted"):
        view.read("snapshot:overview")


def test_effective_config_reads_only_bound_connection_without_calling_it(setup, tmp_path, monkeypatch):
    service, sid, root = setup
    monkeypatch.setenv("CUSTOM_AUTH", "test-secret-not-in-snapshot")
    config = tmp_path / "models.toml"
    config.write_text('[models.main]\nmodel="test"\napi_key_env="CUSTOM_AUTH"\nbase_url="https://example.invalid/v1"\n')
    service.config_path = str(config)
    connector = service.store.external_tools.save(
        {
            "kind": "mcp",
            "name": "Search",
            "description": "Search",
            "config": {"transport": "streamable_http", "url": "https://example.invalid/mcp", "headers_env": {"Authorization": "CUSTOM_AUTH"}},
        }
    )
    state = service.store.snapshot(sid)
    state["task"]["external_tool_bindings"] = [{"connector_id": connector["id"]}]
    manager = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        view, secrets = manager._configuration(state)
        assert "test-secret-not-in-snapshot" in secrets
        assert "test-secret-not-in-snapshot" not in json.dumps(view)
        assert view["models"]["main"]["api_key_present"] is True
        assert view["external_connections"][0]["config"]["headers_env"]["Authorization"] == "CUSTOM_AUTH"
        assert view["external_connections"][0]["credentials_present"]["CUSTOM_AUTH"] is True
    finally:
        manager.close()


def test_budget_errors_identify_every_invalid_field_and_range():
    from loom.diagnostics.jobs import MAXIMUM, budgets

    with pytest.raises(ServiceError) as error:
        budgets({"max_tokens": 10_000_000, "max_duration_seconds": 7200, "max_steps": 0})
    message = str(error.value)
    assert "Tokens (max_tokens): received 10,000,000; allowed integer range is 1–2,000,000" in message
    assert "Seconds (max_duration_seconds): received 7,200; allowed integer range is 1–3,600" in message
    assert "Steps (max_steps): received 0; allowed integer range is 1–100" in message
    assert budgets(MAXIMUM) == MAXIMUM
    for value in [None, True, 1.5, "100"]:
        with pytest.raises(ServiceError, match=r"Model calls \(max_llm_calls\).*allowed integer range is 1–200"):
            budgets({"max_llm_calls": value})


def test_resume_decrease_error_identifies_field_and_previous_total():
    from loom.diagnostics.jobs import DEFAULTS, budgets

    with pytest.raises(ServiceError) as error:
        budgets({"max_tokens": 100, "max_tool_calls": 1}, DEFAULTS)
    assert "Tokens (max_tokens): received 100, previous total 300,000" in str(error.value)
    assert "Tool calls (max_tool_calls): received 1, previous total 100" in str(error.value)


def test_diagnosis_report_word_does_not_select_project_audit(setup, tmp_path):
    from loom.diagnostics.runner import objective
    from loom.tasks.runner import make_task_context

    _, _, root = setup
    directory = snapshot(setup, tmp_path)
    request = TaskRequest(objective=objective('Explain this repo failure'), workspace=root, task_spec=specification(root, directory))
    assembly = TaskAssembly(request, registry=registry_factory())
    context = make_task_context(request, assembly=assembly).unwrap()
    assert context.metadata['profile'] == 'general'
    assert not any('markdown audit' in c.description or 'smoke' in c.description for c in context.identity.constraints)


def test_old_audit_diagnosis_checkpoint_can_resume_after_repair_exhaustion(setup):
    from dataclasses import replace

    from loom.runtime.checkpoints import encode
    from loom.tasks.runner import make_task_context

    service, sid, root = setup
    manager = SessionDiagnosis(service, root=root, provider_factory=factory)
    try:
        job = manager.start(sid, {'mode': 'read_only', 'request_id': 'legacy', 'budgets': {'max_tool_calls': 1}})
        job = wait_job(manager, sid, job['id'])
        assert job['state'] == 'budget_exhausted'
        deadline = time.monotonic() + 5
        while manager.runtime.active and time.monotonic() < deadline:
            time.sleep(.05)
        runtime_sid = manager._load(sid, job['id'])['runtime_session_id']
        def legacy(state, emit, db):
            cp = manager.runtime._checkpoint_value(state)
            audit = make_task_context(TaskRequest(state['task']['objective'], workspace=root, profile='project_audit')).unwrap()
            cp['context'] = replace(cp['context'], identity=audit.identity, goal=audit.goal,
                                    metadata={**cp['context'].metadata, 'profile': 'project_audit'})
            cp['plugin_states']['acceptance'].update(attempts=2, state='blocked', reason='Acceptance repair limit reached',
                                                     goal_digest='legacy-project-audit-goal')
            cp['output_retries'] = 2
            state['run']['checkpoint'] = manager.runtime.store.artifact_in_transaction(
                db, runtime_sid, encode(cp), 'execution_checkpoint')
        manager.runtime.store.update(runtime_sid, legacy)
        manager.resume(sid, job['id'], {'budgets': {'max_tool_calls': 10}})
        done = wait_job(manager, sid, job['id'])
        assert done['state'] == 'completed', done
        assert done['counters']['evidence_reads'] == 1
    finally:
        manager.close()
