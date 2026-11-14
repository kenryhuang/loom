import json
from types import SimpleNamespace

import pytest

from loom.llm import LlmResponse
from loom.tasks.acceptance import DEFAULTS, AcceptanceBlocked
from loom.tasks.acceptance_budget import allocation_plan, effective_limits
from loom.tasks.acceptance_evidence import evidence
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.tasks.test_acceptance import Judge, Sink


def criterion(index, *, command=False, scope=None):
    return {'id': f'c{index}', 'description': f'Outcome {index} is correct',
            'verifier': 'command' if command else 'semantic', 'scope': scope if scope is not None else ['input.txt'],
            'check': {'tool_id': 'process_execute', 'input': {'argv': ['check']}} if command else {}}


async def setup(tmp_path, criteria, config=None):
    (tmp_path / 'input.txt').write_text('initial')
    request = TaskRequest('Verify all outcomes and their consistency', workspace=tmp_path, task_spec={
        'acceptance': config or {}, 'tools': {'collections': ['filesystem', 'shell', 'task_control']},
        'session_environment': {'plugin': 'session', 'resources': [{'id': 'workspace', 'kind': 'directory', 'uri': str(tmp_path)}]},
    })
    assembly = TaskAssembly(request, plan_mode='off')
    assembly.configure_acceptance(Judge(criteria))
    context = make_task_context(request, assembly=assembly, plan_mode='off').unwrap()
    runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink(), cancellation=None)
    await assembly.acceptance.prepare(context, runtime)
    return assembly, context, runtime


def passed(payload):
    reference = payload['evidence'][0]['id']
    return {'results': [{'criterion_id': c['id'], 'status': 'passed', 'reason': 'Supported by inspected evidence',
                         'evidence_ids': [reference], 'gap': None} for c in payload['criteria']]}


@pytest.mark.asyncio
async def test_large_independent_checks_split_before_overflow_and_goal_uses_receipts(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(i, command=True) for i in range(8)])
    payloads = []

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 2000, 'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        payloads.append(payload)
        ids = {c['id'] for c in payload['criteria']}
        assert stage == 'verify'  # Individual checks fit without pagination.
        if ids != {'goal'}:
            assert 1 <= len(ids) <= 3
            assert {e['id'] for e in payload['evidence'] if 'detail' in e} == ids
        else:
            assert len(payload['criterion_results']) == 8
            assert all(r['status'] == 'passed' for r in payload['criterion_results'])
            assert any(e['id'].startswith('verification:') for e in payload['evidence'])
            assert '证' * 2000 not in content
        assert assembly.acceptance.input_budget(prompt, content)['estimated_input_tokens'] < 19660
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        assert await assembly.acceptance.verify(context, runtime, 'All outcomes delivered')
        details = [p for p in payloads if p['criteria'][0]['id'] != 'goal']
        assert len(details) > 1
        assert [p['criteria'][0]['id'] for p in payloads][-1] == 'goal'
        assert len(assembly.acceptance.data['results']) == 9
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_unscoped_criterion_keeps_its_evidence_when_grouped_with_scoped_check(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0), criterion(1, scope=[])])
    (tmp_path / 'extra.txt').write_text('Additional source fact')
    (tmp_path / 'report.md').write_text('Complete report')
    assembly._workspace_reports['report.md'] = {}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        if payload['criteria'][0]['id'] != 'goal':
            assert [c['id'] for c in payload['criteria']] == ['c0', 'c1']
            assert 'Additional source fact' in content
            assert next(e for e in payload['evidence'] if e['id'] == 'report:report.md')['content'] == 'Complete report'
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance_model_request = request
    try:
        await assembly.handlers()['read_file']({'path': 'input.txt'})
        await assembly.handlers()['read_file']({'path': 'extra.txt'})
        assert await assembly.acceptance.verify(context, runtime, 'answer')
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change_file', [False, True])
async def test_completed_batches_survive_checkpoint_and_changed_inputs_invalidate_them(tmp_path, change_file):
    assembly, context, runtime = await setup(tmp_path, [criterion(i, command=True) for i in range(4)], {'batch_max_checks': 2})
    commands, judgments = [], []
    interrupted = True

    async def check(c, runtime):
        commands.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [c['id'] for c in payload['criteria']]
        judgments.append(ids)
        if ids == ['c2', 'c3'] and interrupted:
            raise TimeoutError()
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    restored = None
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert {r['criterion_id'] for r in assembly.acceptance.data['results'] if r['status'] == 'passed'} == {'c0', 'c1'}
        restored = TaskAssembly(assembly.request, plan_mode='off')
        restored.configure_acceptance(Judge())
        restored._local_artifacts = assembly._local_artifacts
        restored.restore(assembly.snapshot())
        restored.acceptance.check = check
        restored.acceptance_model_request = request
        interrupted = False
        if change_file:
            (tmp_path / 'input.txt').write_text('changed')
        assert await restored.acceptance.verify(context, runtime, 'answer')
        assert commands == [f'c{i}' for i in range(4)] * (2 if change_file else 1)
        assert judgments.count(['c0', 'c1']) == (2 if change_file else 1)
        assert judgments[-1] == ['goal']
    finally:
        await assembly.close()
        if restored:
            await restored.close()


def test_explicit_evidence_target_stays_ahead_of_unrelated_scopes():
    paths = [f'file{i:02}.txt' for i in range(64)]
    criteria = [criterion(0, scope=paths[:32]), criterion(1, scope=paths[32:])]
    rows = [dict(id=f'tool:{i}', tool='read_file', ok=True, goal_digest='g', paths=[path], input=path,
                 artifact={'sha256': str(i)}, artifact_format='call') for i, path in enumerate(paths)]
    assembly = SimpleNamespace(_acceptance_calls=rows, acceptance=SimpleNamespace(data={'goal_digest': 'g', 'plan': {'criteria': criteria}},
                               input_budget=lambda: {'max_input_tokens': 600000}), read_artifact=lambda sha: {'output': 'fact'})
    view = evidence(assembly, targets=[paths[0], *paths], expanded=True)
    assert view[1]['id'] == 'tool:0'
    assert view[0]['reviewed_calls'] == 48


@pytest.mark.asyncio
@pytest.mark.parametrize('target', [dict(kind='workspace_path', value='extra.txt'), dict(kind='tool', value='read_file')])
async def test_gap_repair_only_reviews_affected_criterion_and_retains_initial_verdicts(tmp_path, target):
    assembly, context, runtime = await setup(tmp_path, [criterion(0), criterion(1)])
    (tmp_path / 'extra.txt').write_text('decisive extra fact')
    judgments = []
    interrupted = True

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [c['id'] for c in payload['criteria']]
        judgments.append(ids)
        result = passed(payload)
        if ids == ['c0', 'c1']:
            assert 'decisive extra fact' not in content
            result['results'][1].update(status='blocked', gap={'kind': 'missing_evidence',
                'targets': [target], 'required_fact': 'extra fact', 'suggested_action': 'inspect extra.txt'})
        elif ids == ['c1']:
            assert 'decisive extra fact' in content
            if interrupted:
                raise TimeoutError()
        return LlmResponse(json.dumps(result))

    assembly.acceptance_model_request = request
    try:
        await assembly.handlers()['read_file']({'path': 'input.txt'})
        await assembly.handlers()['read_file']({'path': 'extra.txt'})
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert {r['criterion_id']: r['status'] for r in assembly.acceptance.data['results']} == {'c0': 'passed', 'c1': 'blocked'}
        interrupted = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert judgments == [['c0', 'c1'], ['c1'], ['c1'], ['goal']]
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_oversized_single_evidence_is_paged_without_losing_characters(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)])
    fragments, observations, payloads = [], [], []
    output = '证' * 13000 + 'TAIL_ASSERTION'

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'output': output, 'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        payloads.append(payload)
        if stage == 'verify_page':
            for row in payload['evidence']:
                if 'serialized_fragment' in row:
                    fragments.append(row)
            text = 'tail present' if 'TAIL_ASSERTION' in content else 'earlier evidence inspected'
            observations.append(text)
            return LlmResponse(json.dumps({'observations': [{'criterion_id': c['id'], 'facts': text, 'limitations': 'Partial evidence only',
                'evidence_ids': [payload['evidence'][0]['id']]} for c in payload['criteria']]}))
        if payload['criteria'][0]['id'] == 'c0':
            assert all(e.get('assurance') == 'partial_evidence_observations_only' for e in payload['evidence'])
            assert 'tail present' in content
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        before = assembly.acceptance.config['max_model_calls']
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        pieces = sorted((r for r in fragments if r['id'] == 'c0'), key=lambda r: r['fragment']['offset'])
        assert len(pieces) > 1
        assert pieces[0]['fragment']['offset'] == 0
        assert all(a['fragment']['end'] == b['fragment']['offset'] for a, b in zip(pieces, pieces[1:], strict=False))
        restored = json.loads(''.join(r['serialized_fragment'] for r in pieces))
        assert restored['detail']['output'] == output
        assert assembly.acceptance.config['max_model_calls'] > before
        assert assembly.acceptance.public()['budget']['allocation']['evidence_pages'] == len(observations)
        assert payloads[-1]['criteria'][0]['id'] == 'goal'
    finally:
        await assembly.close()


def test_budget_covers_singleton_batches_expansion_protocol_repair_and_all_rounds():
    plan = {'criteria': [criterion(i, command=True) for i in range(8)] + [dict(id='goal', verifier='semantic')]}
    limits = effective_limits(DEFAULTS, {}, plan)
    allocation = allocation_plan(limits, plan)
    assert allocation['review_units'] == 9
    assert limits['max_model_calls'] == 3 + 3 * (9 * 2) * 3 == 165
    assert limits['max_seconds'] == 165 * 120 + 3 * 8 * 60
    paged = effective_limits(DEFAULTS, {}, plan, {'c0': 8})
    assert paged['max_model_calls'] == 165 + 3 * (8 * 2) * 3
    assert paged['max_seconds'] > limits['max_seconds']
    explicit = effective_limits(DEFAULTS, {'max_model_calls': 4, 'max_seconds': 90}, plan, {'c0': 8})
    assert explicit['max_model_calls'] == 4 and explicit['max_seconds'] == 90


def test_large_default_allocation_can_be_retained_as_configuration_on_reassembly():
    plan = {'criteria': [criterion(i, command=True) for i in range(8)] + [dict(id='goal', verifier='semantic')]}
    limits = effective_limits(DEFAULTS, {}, plan, {c['id']: 16 for c in plan['criteria']})
    assert limits['max_seconds'] > 100000
    assembly = TaskAssembly(TaskRequest('Verify', task_spec={'acceptance': limits}), plan_mode='off')
    assert assembly.acceptance.config['max_seconds'] == limits['max_seconds']


@pytest.mark.asyncio
async def test_explicit_budget_stops_before_next_batch_but_keeps_paid_results(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(i) for i in range(4)],
                                             {'batch_max_checks': 2, 'max_model_calls': 2})
    try:
        with pytest.raises(AcceptanceBlocked, match='budget exhausted'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert assembly.acceptance.data['model_calls'] == 2  # planning and first batch
        assert {r['criterion_id'] for r in assembly.acceptance.data['results']} == {'c0', 'c1'}
        assert all(r['status'] == 'passed' for r in assembly.acceptance.data['results'])
        assert assembly.acceptance.data['state'] != 'passed'
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_completed_pages_resume_without_repeating_commands_or_paid_page_reads(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)])
    inspected, commands = [], []
    interrupted = True

    async def check(c, runtime):
        commands.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 10000}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        if stage == 'verify_page':
            index = payload['page']['index']
            inspected.append(index)
            if index == 2 and interrupted:
                raise TimeoutError()
            return LlmResponse(json.dumps({'observations': [{'criterion_id': c['id'], 'facts': 'Inspected this page',
                'limitations': 'One page only', 'evidence_ids': [payload['evidence'][0]['id']]} for c in payload['criteria']]}))
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        allocation = assembly.acceptance.config['max_model_calls']
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupted = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert inspected.count(1) == 1 and inspected.count(2) == 2
        assert commands == ['c0']
        assert assembly.acceptance.config['max_model_calls'] == allocation
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_page_limit_blocks_without_silently_omitting_evidence(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)], {'max_evidence_pages': 1})

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 13000}

    assembly.acceptance.check = check
    try:
        with pytest.raises(AcceptanceBlocked, match='max_evidence_pages') as exc:
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert exc.value.code == 'ACCEPTANCE_INPUT_BUDGET'
        assert assembly.acceptance.data['model_calls'] == 1  # Only planning; no paid truncated review.
        assert assembly.acceptance.data['state'] != 'passed'
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_component_passes_cannot_bypass_independent_goal_failure(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0), criterion(1)])

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        value = passed(payload)
        if payload['criteria'][0]['id'] == 'goal':
            value['results'][0].update(status='failed', reason='Components contradict each other',
                gap={'kind': 'deliverable_mismatch', 'targets': [], 'required_fact': 'Consistent outcomes', 'suggested_action': 'Reconcile outputs'})
        return LlmResponse(json.dumps(value))

    assembly.acceptance_model_request = request
    try:
        assert not await assembly.acceptance.verify(context, runtime, 'answer')
        assert {r['criterion_id']: r['status'] for r in assembly.acceptance.data['results']} == {'c0': 'passed', 'c1': 'passed', 'goal': 'failed'}
        assert not assembly.acceptance.applicable('answer')
    finally:
        await assembly.close()


@pytest.mark.parametrize('field', ['batch_max_checks', 'batch_max_input_tokens', 'max_evidence_pages'])
@pytest.mark.parametrize('value', [0, True, 1.5])
def test_batch_configuration_rejects_invalid_limits(field, value):
    with pytest.raises(ValueError, match='Acceptance limits'):
        TaskAssembly(TaskRequest('Verify', task_spec={'acceptance': {field: value}}), plan_mode='off')


@pytest.mark.asyncio
async def test_repeated_local_reads_do_not_exhaust_pages_or_erase_execution_provenance(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview

    item = {**criterion(0), 'required_tools': ['read_file']}
    assembly, context, runtime = await setup(tmp_path, [item], {'max_evidence_pages': 4})
    (tmp_path / 'input.txt').write_text('stable source fact\n' * 700)
    try:
        for index in range(136):
            await assembly.handlers()['read_file']({'path': 'input.txt'}, {'metadata': {'tool_call_id': f'read-{index}'}})
        source = evidence(assembly, criteria=[item], full=True)
        assert len(assembly._acceptance_calls) == 136
        assert source[0]['reviewed_calls'] == 136 and source[0]['omitted_calls'] == 0
        assert source[0]['deduplicated_calls'] == 135 and source[0]['evidence_items'] == 1
        assert len(source[1]['equivalent_call_ids']) == 136
        assert not source[1]['truncated']
        batch = BatchReview(assembly.acceptance, runtime, [], {}, {}, None)
        assert len(batch.pages([item], source, 2000)) <= 4
        # Execution-count obligations still use all distinct journal invocations.
        count = {**item, 'verifier': 'tool_call', 'check': {'tool_id': 'read_file', 'min_calls': 136}}
        result, detail = await assembly.acceptance.check(count, runtime)
        assert result['status'] == 'passed' and detail['successful'] == 136
        restored = TaskAssembly(assembly.request, plan_mode='off')
        try:
            restored.configure_acceptance(Judge())
            restored._local_artifacts = assembly._local_artifacts
            restored.restore(assembly.snapshot())
            assert evidence(restored, criteria=[item], full=True) == source
        finally:
            await restored.close()
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', [[], ['input.txt']])
async def test_command_review_uses_fresh_check_and_only_retrieves_old_commands_for_explicit_gaps(tmp_path, scope):
    from loom.tasks.acceptance_batches import BatchReview

    item = {**criterion(0, command=True, scope=scope), 'required_tools': []}
    assembly, context, runtime = await setup(tmp_path, [item])
    try:
        for index in range(40):
            ref = assembly.publish_artifact({'output': 'unrelated shell log ' * 5000}, 'acceptance_tool_evidence')
            assembly._acceptance_calls.append(dict(id=f'tool:old-{index}', tool='process_execute', ok=True,
                goal_digest=assembly.acceptance.data['goal_digest'], paths=[], input=f'old command {index}', artifact=ref, artifact_format='call'))
        fresh = {'id': 'c0', 'result': {'criterion_id': 'c0', 'status': 'pending'}, 'detail': {'exit_code': 0, 'stdout': 'fresh check result'}}
        batch = BatchReview(assembly.acceptance, runtime, [fresh], {}, {}, None)
        view = batch.view([item])
        assert next(e for e in view if e['id'] == 'c0')['detail']['stdout'] == 'fresh check result'
        assert not any(e.get('tool') == 'process_execute' for e in view)
        expanded = batch.view([item], target_tools=['process_execute'], expanded=True)
        assert len([e for e in expanded if e.get('tool') == 'process_execute']) == 40
    finally:
        await assembly.close()


def test_read_coalescing_preserves_versions_ranges_failures_and_non_file_operations():
    from loom.tasks.acceptance_evidence import coalesce_reads

    base = dict(tool='read_file', ok=True, artifact={'sha256': 'original'}, input='whole file', paths=['a.txt'], dependencies={'a.txt': 'v1'})
    rows = [dict(base, id='one'), dict(base, id='two'), dict(base, id='new', dependencies={'a.txt': 'v2'}),
            dict(base, id='range', input='bytes 100..200'), dict(base, id='error', ok=False),
            dict(base, id='cmd1', tool='shell_execute'), dict(base, id='cmd2', tool='shell_execute'),
            dict(base, id='ext1', external=True), dict(base, id='ext2', external=True)]
    selected = coalesce_reads(rows)
    assert len(selected) == len(rows) - 1
    assert selected[0]['equivalent_call_ids'] == ['one', 'two']
    assert 'equivalent_call_ids' not in rows[0]
    assert {r['id'] for r in selected[1:]} == {r['id'] for r in rows[2:]}


@pytest.mark.asyncio
async def test_long_page_observations_are_reduced_without_format_retry_or_dropping_receipts(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)], {'batch_max_input_tokens': 6000})
    stages = []
    reduced = []

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0, 'stdout': 'fact ' * 7000}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        stages.append(stage)
        if stage == 'verify_page':
            indirect = any(e['id'].startswith('verification:') for e in payload['evidence'])
            reduced.append(indirect)
            facts = 'Combined facts from every supplied receipt' if indirect else 'fact ' * 2000
            return LlmResponse(json.dumps({'observations': [{'criterion_id': c['id'], 'facts': facts,
                'limitations': 'Observation only', 'evidence_ids': [e['id'] for e in payload['evidence']]}
                for c in payload['criteria']]}))
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        assert await assembly.acceptance.verify(context, runtime, 'candidate')
        assert any(reduced) and not all(reduced)
        assert not any('repair' in stage for stage in stages)
        manifest = assembly.read_artifact(assembly.acceptance.data['evidence_manifest_ref']['sha256'])
        # Every original observation is still reachable in the final manifest.
        observations = [assembly.read_artifact(e['artifact']['sha256']) for e in manifest['evidence']
                        if e['id'].startswith('verification:') and e.get('artifact')]
        assert any(any(r.get('facts') == 'fact ' * 2000 for r in item.get('results', [])) for item in observations)
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_overlapping_views_read_immutable_artifacts_once_but_recheck_file_freshness(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview

    items = [criterion(0), criterion(1)]
    assembly, _, runtime = await setup(tmp_path, items)
    await assembly.handlers()['read_file']({'path': 'input.txt'})
    backend = assembly.read_artifact
    reads = []

    def read(sha):
        reads.append(sha)
        return backend(sha)

    assembly.read_artifact = read
    batch = BatchReview(assembly.acceptance, runtime, [], {}, {}, None)
    try:
        for group in ([items[0]], items, [items[1]]):
            assert any('initial' in (r.get('output') or '') for r in batch.view(group))
        assert len(reads) == 1
        (tmp_path / 'input.txt').write_text('changed after acquisition')
        view = batch.view(items)
        source = next(r for r in view if r['id'].startswith('tool:'))
        assert source['stale'] and source['output'] is None
        assert len(reads) == 1
    finally:
        await assembly.close()
