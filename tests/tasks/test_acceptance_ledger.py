"""Recovery invariants, exercised with fresh controller state and no model history."""

import json

import pytest

from loom.llm import LlmResponse
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.acceptance_ledger import VerificationLedger
from tests.tasks.test_acceptance_batches import criterion, passed, setup


def answer(payload, stage):
    if stage == 'verify_page':
        return {'observations': [{'criterion_id': c['id'], 'facts': 'Observed evidence', 'limitations': 'Partial page',
                                 'evidence_ids': [payload['evidence'][0]['id']]} for c in payload['criteria']]}
    return passed(payload)


@pytest.mark.asyncio
async def test_deterministic_work_survives_interruption_before_semantic_pending_exists(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(i, command=True) for i in range(2)])
    calls = []
    interrupt = True

    async def check(c, runtime):
        calls.append(c['id'])
        if c['id'] == 'c1' and interrupt:
            raise AcceptanceBlocked('Interrupted', code='ACCEPTANCE_INTERRUPTED')
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        return LlmResponse(json.dumps(answer(json.loads(content), stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='Interrupted'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert 'pending_verification' not in assembly.acceptance.data
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert calls == ['c0', 'c1', 'c1']
        progress = assembly.acceptance.public()['verification_progress']
        assert progress['work']['check'] == {'committed': 2, 'total': 2}
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_page_cursor_survives_batch_size_change_without_replanning(tmp_path, monkeypatch):
    from loom.tasks.acceptance_batches import BatchReview

    assembly, context, runtime = await setup(tmp_path, [criterion(i, command=True) for i in range(3)], {'batch_max_checks': 1})
    seen = []
    interrupt = True

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 12000}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        key = (tuple(c['id'] for c in payload['criteria']), stage, payload.get('page', {}).get('index'))
        seen.append(key)
        if key == (('c0',), 'verify_page', 2) and interrupt:
            raise TimeoutError()
        return LlmResponse(json.dumps(answer(payload, stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        saved = assembly.acceptance.snapshot()
        assembly.acceptance.restore(saved)
        assembly.acceptance._configured_limits['batch_max_checks'] = 3
        original = BatchReview.pages

        def pages(self, criteria, evidence, chars):
            assert [c['id'] for c in criteria] != ['c0'], 'Recovery must load the saved chunk plan'
            return original(self, criteria, evidence, chars)

        monkeypatch.setattr(BatchReview, 'pages', pages)
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert seen.count((('c0',), 'verify_page', 1)) == 1
        assert seen.count((('c0',), 'verify_page', 2)) == 2
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_budget_admits_one_page_and_resume_keeps_paid_progress(tmp_path):
    from loom.tasks.acceptance_budget import renew_on_resume

    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)], {'max_model_calls': 2})
    pages = []

    async def check(c, runtime):
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 12000}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        if stage == 'verify_page':
            pages.append(payload['page']['index'])
        return LlmResponse(json.dumps(answer(payload, stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked) as exc:
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert exc.value.code == 'ACCEPTANCE_BUDGET_EXHAUSTED'
        assert pages == [1]  # One planning request and one useful observation.
        assembly.acceptance.data['state'] = 'blocked'
        assembly.acceptance._configured_limits['max_model_calls'] = 20
        await renew_on_resume(assembly.acceptance, 'resume-1', runtime)
        assert assembly.acceptance.data['archived_model_calls'] == 2
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert pages.count(1) == 1
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_completed_response_commits_before_event_delivery(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    calls = []
    saved = []
    original = assembly.acceptance.emit

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        calls.append(stage)
        return LlmResponse(json.dumps(answer(json.loads(content), stage)))

    async def emit(runtime, kind, **fields):
        if kind == 'verification.batch.completed' and not saved:
            saved.append(assembly.acceptance.snapshot())
            raise OSError('event transport failed')
        return await original(runtime, kind, **fields)

    assembly.acceptance_model_request = request
    assembly.acceptance.emit = emit
    try:
        with pytest.raises(OSError, match='event transport'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assembly.acceptance.restore(saved[0])
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert calls == ['verify', 'verify']  # criterion and independent goal only
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_work_retry_limit_survives_restore_and_never_passes(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)], {'max_work_attempts': 2})
    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    assembly.acceptance_model_request = request
    try:
        for _ in range(2):
            with pytest.raises(AcceptanceBlocked, match='timed out'):
                await assembly.acceptance.verify(context, runtime, 'answer')
            assembly.acceptance.restore(assembly.acceptance.snapshot())
        with pytest.raises(AcceptanceBlocked) as exc:
            await assembly.acceptance.verify(context, runtime, 'answer')
        assert exc.value.code == 'ACCEPTANCE_NO_PROGRESS'
        assert len(calls) == 2
        assert assembly.acceptance.data['state'] != 'passed'
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_receipt_dependency_validation_is_transitive(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    ledger = VerificationLedger(assembly.acceptance, assembly.acceptance.fingerprint('answer'))
    try:
        source = assembly.publish_artifact({'content': 'source'}, 'evidence')
        observation = assembly.publish_artifact({'artifact': source, 'facts': 'source fact'}, 'verification_observation')
        item = ledger.item('verify', 'criterion', {})
        ledger.start(item)
        ledger.commit(item, {'artifact': observation, 'value': 'passed'})
        assert ledger.result(item)['value'] == 'passed'
        original = assembly.read_artifact

        def read(sha):
            if sha == source['sha256']:
                raise OSError('lost source')
            return original(sha)

        assembly.read_artifact = read
        with pytest.raises(AcceptanceBlocked, match='artifact is unavailable'):
            ledger.result(item)
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_implicit_page_window_is_not_a_total_evidence_limit(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview

    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    try:
        batch = BatchReview(assembly.acceptance, runtime, [], {}, assembly.acceptance.fingerprint('answer'), None)
        batch.fits = lambda prompt, payload, protocol: len(payload['evidence']) <= 1
        sources = [{'id': f'source-{i}', 'content': str(i)} for i in range(30)]
        pages = batch.pages([criterion(0)], sources, 2000)
        assert len(pages) == 30
        assert [page[0] for page in pages] == sources
        assembly.acceptance._configured_limits['max_evidence_pages'] = 16
        with pytest.raises(AcceptanceBlocked, match='max_evidence_pages'):
            batch.pages([criterion(0)], sources, 2000)
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_dynamic_check_does_not_erase_independent_file_check(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True), criterion(1, command=True, scope=[])])
    calls = []
    interrupt = True

    async def check(c, runtime):
        calls.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        if interrupt:
            raise TimeoutError()
        return LlmResponse(json.dumps(answer(json.loads(content), stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert calls == ['c0', 'c1', 'c1']
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_local_file_change_invalidates_only_dependent_execution(tmp_path):
    (tmp_path / 'other.txt').write_text('other')
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True), criterion(1, command=True, scope=['other.txt'])])
    calls = []
    interrupt = True

    async def check(c, runtime):
        calls.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        if interrupt:
            raise TimeoutError()
        return LlmResponse(json.dumps(answer(json.loads(content), stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'answer')
        (tmp_path / 'input.txt').write_text('changed')
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert calls == ['c0', 'c1', 'c0']
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_oversized_catalogue_searches_every_partition_and_resumes(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview
    from loom.tasks.acceptance_selection import select_evidence

    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    seen = []
    interrupt = True
    sources = [{'id': f'tool:{i}', 'tool': 'read_file', 'paths': [f'{i}.txt'], 'ok': True, 'output': 'fact'} for i in range(12)]

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [r['id'] for r in payload['source_catalogue']]
        seen.append(ids)
        if ids[0] == 'tool:3' and interrupt:
            raise TimeoutError()
        return LlmResponse(json.dumps({'evidence_ids': [ids[0]], 'reason': 'Relevant source'}))

    def batch():
        value = BatchReview(assembly.acceptance, runtime, [], {}, assembly.acceptance.fingerprint('answer'), None)
        value.fits = lambda prompt, payload, protocol: 'source_catalogue' in payload and len(payload['source_catalogue']) <= 3
        return value

    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await select_evidence(batch(), [criterion(0)], sources)
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        chosen = await select_evidence(batch(), [criterion(0)], sources)
        assert seen.count(['tool:0', 'tool:1', 'tool:2']) == 1
        assert {i for group in seen for i in group} == {r['id'] for r in sources}
        inventory = next(r for r in chosen if r['id'] == 'evidence-selection')
        assert inventory['source_count'] == 12 and inventory['selected_count'] == 4
        assert inventory['omitted_count'] == 8
        assert inventory['assurance'] == 'source_selection_only_not_a_verdict'
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_more_than_sixteen_pages_complete_across_budget_resumes(tmp_path):
    from loom.tasks.acceptance_budget import renew_on_resume

    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True)],
                                             {'max_model_calls': 6, 'max_chunks_per_slice': 2})
    inspected = []
    executions = []

    async def check(c, runtime):
        executions.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'output': '证' * 100000 + 'FINAL_EVIDENCE'}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        if stage == 'verify_page':
            inspected.append((payload['page']['index'], payload['page']['total'], 'FINAL_EVIDENCE' in content))
        return LlmResponse(json.dumps(answer(payload, stage)))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        for attempt in range(20):
            try:
                assert await assembly.acceptance.verify(context, runtime, 'answer')
                break
            except AcceptanceBlocked as exc:
                assert exc.code == 'ACCEPTANCE_BUDGET_EXHAUSTED'
                assembly.acceptance.data['state'] = 'blocked'
                assembly.acceptance.restore(assembly.acceptance.snapshot())
                await renew_on_resume(assembly.acceptance, f'resume-{attempt}', runtime)
        else:
            pytest.fail('Stable finite evidence did not converge')
        assert len(inspected) > 16
        assert [i for i, _, _ in inspected] == list(range(1, len(inspected) + 1))
        assert all(total == len(inspected) for _, total, _ in inspected)
        assert inspected[-1][2]
        assert executions == ['c0']
        assert assembly.acceptance.public()['budget']['total_model_calls'] == len(inspected) + 3
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_untrusted_source_artifact_fields_are_not_dependencies(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    try:
        ledger = VerificationLedger(assembly.acceptance, assembly.acceptance.fingerprint('answer'))
        source = assembly.publish_artifact({'artifact': {'sha256': 'not-a-host-reference'}}, 'acceptance_tool_evidence')
        ledger.validate_sources({'artifact': source})
        item = ledger.item('verify', 'stable-work-id', {'prompt': 'v1'})
        ledger.start(item)
        ledger.commit(item, {'artifact': source, 'value': 'old response'})
        assert ledger.result(item)['value'] == 'old response'
        revised = ledger.item('verify', 'stable-work-id', {'prompt': 'v2'})
        assert revised['id'] == item['id']
        assert ledger.result(revised) is None  # Stable identity is not permission to reuse changed questions.
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_goal_uses_receipts_and_loads_originals_only_for_explicit_gap(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview

    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    try:
        await assembly.handlers()['read_file']({'path': 'input.txt'})
        gate = assembly.acceptance
        binding = gate.fingerprint('answer')
        batch = BatchReview(gate, runtime, [{'id': 'candidate', 'content': 'answer'}], {}, binding, None)
        goal = [c for c in gate.data['plan']['criteria'] if c['id'] == 'goal']
        proof = assembly.publish_artifact({'results': [{'criterion_id': 'c0', 'status': 'passed'}]}, 'verification_evidence')
        receipts = [{'id': 'verification:detail', 'artifact': proof, 'results': [{'criterion_id': 'c0', 'status': 'passed'}]}]
        view = batch.view(goal, receipts=receipts)
        assert not any(r['id'].startswith('tool:') for r in view)
        assert any(r['id'] == 'candidate' for r in view)
        assert any(r['id'] == 'verification:detail' for r in view)
        directory = next(r for r in view if r['id'] == 'verification:source-directory')
        assert directory['source_count'] == 1
        index = assembly.read_artifact(directory['artifact']['sha256'])
        assert index['sources'][0]['source_digest']
        assert 'artifact' not in index['sources'][0]  # Directory entries are locators, not proof dependencies.
        expanded = batch.view(goal, receipts=receipts, expanded=True, targets=['input.txt'])
        assert any(r['id'].startswith('tool:') and 'initial' in r.get('output', '') for r in expanded)
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_task_call_limit_does_not_consume_acceptance_calls_or_work_retries(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    assembly._local_calls = 9
    assembly.acceptance_limits = {'max_llm_calls': 9, 'max_tokens': None}
    gate = assembly.acceptance
    calls_before = gate.data['model_calls']
    try:
        for _ in range(4):
            with pytest.raises(AcceptanceBlocked) as exc:
                await gate.verify(context, runtime, 'answer')
            assert exc.value.code == 'MODEL_CALL_BUDGET_EXCEEDED'
            assert '9/9' in str(exc.value)
            gate.restore(gate.snapshot())
        assert gate.data['model_calls'] == calls_before
        items = list(gate.data['verification_ledger']['items'].values())
        assert len(items) == 1 and items[0]['state'] == 'waiting_budget' and items[0]['attempts'] == 0
        assert gate.public()['task_budget']['model_calls'] == 9
        assert assembly._local_calls == 9
    finally:
        await assembly.close()


def test_task_call_limit_recovery_requires_budget_change_not_blind_resume():
    from loom.tasks.recovery import assess

    state = {'task': {'objective': 'Verify', 'revision': 1}, 'run': {'id': 'r'},
             'acceptance': {'state': 'blocked', 'cause_code': 'MODEL_CALL_BUDGET_EXCEEDED'}}
    recovery = assess(state)
    assert recovery['mode'] == 'manual'
    assert any(c['code'] == 'MODEL_CALL_BUDGET_EXCEEDED' for c in recovery['causes'])


@pytest.mark.asyncio
async def test_resume_upgrades_only_legacy_goal_view_and_keeps_component_receipts(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    seen = []
    interrupt = True

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [c['id'] for c in payload['criteria']]
        seen.append(ids)
        if ids == ['goal']:
            if interrupt:
                raise TimeoutError()
            assert not any(r['id'].startswith('tool:') for r in payload['evidence'])
            assert any(r['id'].startswith('verification:') and r.get('results') for r in payload['evidence'])
        return LlmResponse(json.dumps(answer(payload, stage)))

    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked, match='timed out'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        pending = assembly.acceptance.data['pending_verification']
        job = next(j for j in pending['batches'].values() if j['phase'] == 'initial')
        job.pop('view_version')
        old = assembly.read_artifact(job['view_ref']['sha256'])
        job['view_ref'] = assembly.publish_artifact([*old, {'id': 'tool:legacy', 'output': 'large historical source'}], 'verification_batch_view')
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert seen.count(['c0']) == 1
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_page_progress_is_durable_and_does_not_claim_criterion_pass(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    gate = assembly.acceptance
    try:
        VerificationLedger(gate, gate.fingerprint('answer'))
        before = gate.data['model_calls']
        await gate.emit(runtime, 'verification.batch.started', criterion_ids=['c0'], stage='verify_page', page={'index': 2, 'total': 6})
        gate.restore(gate.snapshot())
        progress = gate.public()['verification_progress']['current']
        assert progress == {'stage': 'verify_page', 'criterion_ids': ['c0'], 'state': 'running', 'page': {'index': 2, 'total': 6}}
        await gate.emit(runtime, 'verification.batch.completed', criterion_ids=['c0'], stage='verify_page', page={'index': 2, 'total': 6})
        assert gate.public()['verification_progress']['current']['state'] == 'saved'
        assert gate.data['results'] == []
        assert gate.data['model_calls'] == before
        assert runtime.trace_sink.events[-1]['acceptance']['verification_progress']['current']['page']['total'] == 6
    finally:
        await assembly.close()
