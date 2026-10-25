import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom.core import ok
from loom.llm import build_messages
from loom.memory.runtime import RuntimeMemory
from loom.service.controller import LoomService
from loom.service.store import SessionStore
from loom.tasks.assembly import TaskAssembly
from loom.tasks.cli import parse_task_cli_args, run_task_cli
from loom.tasks.request import TaskHarness, TaskRequest
from loom.tasks.runner import make_task_context
from tests.memory.helpers import MemoryProvider, memory_provider_factory, seed
from tests.service.test_controller import wait_state
from tests.tasks.test_task_runner import RecordingTraceSink


def test_real_cli_to_assembly_memory_tools_and_trace(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    config = tmp_path / 'config.yaml'
    config.write_text('models: {}\n')
    provider = MemoryProvider()
    monkeypatch.setattr('loom.tasks.runner._create_provider', lambda *a, **k: ok(provider))
    trace = tmp_path / 'smoke.jsonl'
    options = parse_task_cli_args(['Fix duplicate planets', '--workspace', str(tmp_path), '--config', str(config),
                                  '--plan-mode', 'off', '--memory-store', str(store.directory), '--trace-path', str(trace)])
    assert options.request.metadata['memory_database'] == str(store.path)
    result = asyncio.run(run_task_cli(options)).unwrap()
    assert 'original citations' in result.output
    observations = result.run_result.context.state.observations
    assert {'memory_search', 'memory_read'} <= {o.source for o in observations}
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any('memory.retrieved' in json.dumps(event) for event in events)
    assert any('content_digest' in json.dumps(event) for event in events)
    assert len(provider.seen) == 3
    # Historical data is never elevated into the system message.
    assert all('eval-memory' not in messages[0].content for messages in provider.seen)


@pytest.mark.parametrize('dynamic', [False, True])
def test_durable_worker_uses_same_memory_and_journals_read_only(tmp_path, dynamic):
    workspace = tmp_path / 'solar'
    workspace.mkdir()
    service = LoomService(tmp_path / 'data', provider_factory=memory_provider_factory)
    seed(service.store, workspace)
    service.start()
    try:
        payload = {'objective': 'Fix duplicate planets', 'workspace': str(workspace), 'plan_mode': 'off'}
        if dynamic:
            payload['task_spec'] = {
                'session_environment': {'plugin': 'session', 'resources': [
                    {'id': 'code', 'kind': 'directory', 'uri': str(workspace), 'access': 'read-write'}]},
                'tools': {'collections': ['task_control']}, 'workflow': {'plugin': 'dynamic'},
            }
        sid = service.create('current-task', payload)['session_id']
        state = wait_state(service, sid, 'idle')
        assert state['run']['state'] == 'completed'
        assert 'original citations' in state['messages'][-1]['content']
        with service.store.transaction() as db:
            events = [json.loads(r[0]) for r in db.execute('SELECT body FROM events WHERE session_id=?', (sid,))]
            operations = [json.loads(r[0]) for r in db.execute('SELECT body FROM operations WHERE session_id=?', (sid,))]
        retrievals = [e for e in events if e['type'] == 'memory.retrieved']
        assert retrievals and 'eval-memory' in json.dumps(retrievals)
        reads = [op for op in operations if op['call']['name'] in {'memory_read', 'memory_search'}]
        assert len(reads) == 2 and all(op['effect_kind'] == 'read_only' for op in reads)
    finally:
        service.close()


def test_attach_bounded_refresh_error_and_harness(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    memory = RuntimeMemory(store.path, workspace=tmp_path)
    context = make_task_context(TaskRequest('Fix duplicate planets', workspace=tmp_path), plan_mode='off').unwrap()
    sink = RecordingTraceSink()
    runtime = SimpleNamespace(trace_sink=sink, trace_id='trace')
    async def run():
        first = await memory.attach(context, runtime)
        second = await memory.attach(first, runtime)
        assert first.metadata == second.metadata
        assert len(sink.events) == 1
        assert 'eval-memory' in next(m.content for m in build_messages(first) if m.name == 'deep_eval_memory')
        changed = replace(second, goal=replace(second.goal, objective='unrelated payments'))
        changed = await memory.attach(changed, runtime)
        assert 'retrieved_memory' not in changed.metadata
        missing = RuntimeMemory(tmp_path / 'missing.sqlite', workspace=tmp_path)
        after = await missing.attach(first, runtime)
        assert 'retrieved_memory' not in after.metadata
        assert sink.events[-1]['type'] == 'memory.unavailable'
        assert sink.events[-1]['code'] == 'memory.store_unavailable'
    asyncio.run(run())
    assembly = TaskAssembly(TaskRequest('planets', workspace=tmp_path, metadata={'memory_database': str(store.path)}),
                            harness=TaskHarness(allowed_tools=('read_file',)))
    try:
        assert not ({'memory_search', 'memory_read'} & {r.id for r in assembly.context_tools()})
    finally:
        asyncio.run(assembly.close())


def test_old_plugin_checkpoint_restores_with_memory_and_pending_exchange_is_unchanged(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    old = TaskAssembly(TaskRequest('planets', workspace=tmp_path), plan_mode='off')
    snapshot = old.snapshot()
    bridge = SimpleNamespace(checkpoint={'pending': True}, binding_effects={})
    current = TaskAssembly(TaskRequest('planets', workspace=tmp_path, metadata={'memory_database': str(store.path)}),
                           plan_mode='off', execution=bridge)
    try:
        current.restore(snapshot)
        assert {'memory_read', 'memory_search'} <= set(current.handlers())
        # Task wrapper must pass a restored exchange through without retrieving or changing its metadata.
        from loom.core import err, make_loom_error
        async def stop(context, runtime):
            assert 'retrieved_memory' not in context.metadata
            return err(make_loom_error('test.stop', 'stop', retryable=False))
        from loom.tasks.runner import make_task_loop
        definition = replace(make_task_loop(TaskRequest('planets', workspace=tmp_path), MemoryProvider()), step=stop)
        context = make_task_context(current.request, assembly=current).unwrap()
        runtime = SimpleNamespace(trace_sink=RecordingTraceSink(), trace_id='trace')
        result = asyncio.run(current.wrap_loop(definition).step(context, runtime))
        assert result.error.code == 'test.stop'
        assert not runtime.trace_sink.events
        assert current.memory.query is None
    finally:
        asyncio.run(old.close())
        asyncio.run(current.close())


def test_resume_loads_memory_created_while_waiting_without_replaying_tools(tmp_path):
    from tests.memory.helpers import paused_memory_provider_factory
    from tests.service.test_controller import command

    service = LoomService(tmp_path / 'data', provider_factory=paused_memory_provider_factory).start()
    try:
        sid = service.create('waiting', {'objective': 'Fix duplicate planets', 'workspace': str(tmp_path), 'plan_mode': 'off'})['session_id']
        waiting = wait_state(service, sid, 'awaiting_input')
        seed(service.store, tmp_path)
        command(service, sid, 'answer_input', request_id=waiting['input_request']['id'], answer='Continue')
        state = wait_state(service, sid, 'idle')
        assert 'newly evaluated memory' in state['messages'][-1]['content']
        with service.store.transaction() as db:
            events = [json.loads(r[0]) for r in db.execute('SELECT body FROM events WHERE session_id=?', (sid,))]
        matched = [e for e in events if e['type'] == 'memory.retrieved' and 'eval-memory' in json.dumps(e)]
        assert matched
        questions = [e for e in events if e['type'] == 'tool.requested' and 'request_input' in json.dumps(e)]
        assert len(questions) <= 1
    finally:
        service.close()


def test_memory_tools_do_not_displace_required_execution_tools_under_budget(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    spec = {
        'session_environment': {'plugin': 'session', 'resources': [
            {'id': 'code', 'kind': 'directory', 'uri': str(tmp_path), 'access': 'read-write'}]},
        'tools': {'collections': ['filesystem', 'task_control'], 'budget': {'max_tools': 4}},
        'workflow': {'plugin': 'legacy_planning', 'mode': 'off'},
    }
    request = TaskRequest('Fix duplicate planets', workspace=tmp_path, task_spec=spec)
    baseline = TaskAssembly(request)
    current = TaskAssembly(replace(request, metadata={'memory_database': str(store.path)}))
    try:
        context = make_task_context(request, assembly=current).unwrap()
        runtime = SimpleNamespace(trace_sink=RecordingTraceSink(), trace_id='trace')
        attached = asyncio.run(current.prepare_memory(context, runtime))
        expected = {r.id for r in baseline.resolve_tools(make_task_context(request, assembly=baseline).unwrap())}
        assert {r.id for r in current.resolve_tools(attached)} == expected
    finally:
        asyncio.run(baseline.close())
        asyncio.run(current.close())
