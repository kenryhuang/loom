"""Cross-batch source routing and reference integrity across fresh controllers."""

import json

import pytest

from loom.llm import LlmResponse
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.acceptance_protocols import validate_verification
from loom.tasks.acceptance_sources import citation_view, resolve_citations
from loom.tasks.assembly import TaskAssembly
from tests.tasks.test_acceptance import Judge
from tests.tasks.test_acceptance_batches import criterion, passed, setup


def receipt(assembly, identifier, command, stdout):
    ref = assembly.publish_artifact({'input': {'command': command, 'cwd': '.'}, 'output': {'stdout': stdout, 'exit_code': 0}},
                                    'acceptance_tool_evidence')
    assembly._acceptance_calls.append(dict(id=identifier, tool='shell_execute', ok=True,
        goal_digest=assembly.acceptance.data['goal_digest'], paths=['.'], input=json.dumps({'command': command, 'cwd': '.'}),
        artifact=ref, artifact_format='call'))
    return ref


@pytest.mark.asyncio
async def test_cross_batch_commands_and_shell_documents_survive_fresh_controller(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, command=True), criterion(1), criterion(2)], {'batch_max_checks': 1})
    source_id = 'tool:' + 'actual-source-identity-' * 8
    receipt(assembly, source_id, 'cat docs/baseline.md', 'baseline chunk = 123; previous generation')
    commands, judgments = [], []
    interrupted = True
    restored = None

    async def check(c, runtime):
        commands.append(c['id'])
        return {'status': 'passed', 'reason': 'Executed'}, {'exit_code': 0, 'stdout': 'current chunk = 124'}

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [c['id'] for c in payload['criteria']]
        judgments.append(ids)
        if ids == ['c1'] and interrupted:
            raise TimeoutError()
        answer = passed(payload)
        if 'c1' in ids or 'c2' in ids:
            assert 'current chunk = 124' in content  # Fresh check from a different batch.
            assert 'baseline chunk = 123' in content  # Shell cwd='.' does not exclude the source.
            source = next(e for e in payload['evidence'] if e.get('tool') == 'shell_execute')
            assert source['id'] != source_id and source['id'] in payload['citation_ids']
            for row in answer['results']:
                row['evidence_ids'] = [source['id'], 'c0']
        return LlmResponse(json.dumps(answer))

    assembly.acceptance.check = check
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'Complete report')
        pending = assembly.acceptance.data['pending_verification']
        directory = pending['source_directory']
        assert directory and assembly.acceptance.data['results'][0]['status'] == 'passed'
        restored = TaskAssembly(assembly.request, plan_mode='off')
        restored.configure_acceptance(Judge())
        restored._local_artifacts = assembly._local_artifacts
        restored.restore(assembly.snapshot())
        restored.acceptance._configured_limits['batch_max_checks'] = 3
        restored.acceptance.check = check
        restored.acceptance_model_request = request
        interrupted = False
        assert await restored.acceptance.verify(context, runtime, 'Complete report')
        assert commands == ['c0'] and judgments.count(['c0']) == 1
        assert judgments.count(['c1']) == 2
        for row in restored.acceptance.data['results']:
            if row['criterion_id'] in {'c1', 'c2'}:
                assert row['evidence_ids'] == [source_id, 'c0']
                manifest = restored.read_artifact(row['judgment_artifact']['sha256'])
                assert source_id in manifest['citation_aliases'].values()
        discovery = [i for i in restored.acceptance.data['verification_ledger']['items'].values() if i['kind'] == 'discover_sources']
        assert len(discovery) == 1 and discovery[0]['state'] == 'committed'
    finally:
        await assembly.close()
        if restored:
            await restored.close()


@pytest.mark.asyncio
async def test_large_directory_selects_before_loading_originals_and_pins_source_id_gap(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    for index in range(40):
        receipt(assembly, f'tool:source-{index}', f'inspect source {index}', f'original source {index}: ' + '证' * 300)
    original = assembly.read_artifact
    source_digests = {r['artifact']['sha256'] for r in assembly._acceptance_calls}
    reads, selections, judgments = [], [], []
    interrupt = True

    def read(sha):
        if sha in source_digests:
            reads.append(sha)
        return original(sha)

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        if stage == 'select_evidence':
            if not selections:
                assert not reads  # Catalogue metadata is sufficient to select originals.
            selections.append(payload.get('evidence_request'))
            return LlmResponse(json.dumps({'evidence_ids': ['tool:source-0'], 'reason': 'Initial source'}))
        judgments.append(payload['criteria'][0]['id'])
        value = passed(payload)
        if payload['criteria'][0]['id'] == 'c0':
            if any(r['id'] == 'tool:source-17' for r in payload['evidence']):
                assert 'original source 17' in content
                if interrupt:
                    raise TimeoutError()
            else:
                value['results'][0].update(status='blocked', gap=dict(kind='insufficient_evidence_view',
                    targets=[dict(kind='source', value='tool:source-17')], required_fact='Complementary original', suggested_action='Read source-17'))
        return LlmResponse(json.dumps(value))

    assembly.read_artifact = read
    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'report')
        assert len(selections) == 2
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        interrupt = False
        assert await assembly.acceptance.verify(context, runtime, 'report')
        assert len(selections) == 2  # Neither selection nor initial judgment is repeated.
        assert judgments == ['c0', 'c0', 'c0', 'goal']
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_missing_directory_source_blocks_restore_before_paid_judgment(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0)])
    ref = receipt(assembly, 'tool:source', 'cat evidence.txt', 'observed fact')
    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'report')
        assembly.acceptance.restore(assembly.acceptance.snapshot())
        original = assembly.read_artifact

        def read(sha):
            if sha == ref['sha256']:
                raise OSError('lost source')
            return original(sha)

        assembly.read_artifact = read
        with pytest.raises(AcceptanceBlocked) as exc:
            await assembly.acceptance.verify(context, runtime, 'report')
        assert exc.value.code == 'ACCEPTANCE_EVIDENCE_UNAVAILABLE'
        assert calls == [1]
    finally:
        await assembly.close()


def test_citation_aliases_are_bijective_and_nested_provenance_cannot_authorize_a_reference():
    original_id = 'verification:' + 'a' * 64
    evidence = [{'id': original_id, 'observations': [{'criterion_id': 'c0', 'facts': 'partial fact',
                                                   'evidence_ids': ['tool:earlier-page-only']}]},
                {'id': 'verification:ref_1', 'content': 'Existing ID must not collide'}]
    wire, aliases = citation_view(evidence)
    assert len({r['id'] for r in wire}) == 2
    assert wire[0]['id'] == 'verification:ref_1_'
    assert 'evidence_ids' not in wire[0]['observations'][0]
    row = dict(criterion_id='c0', status='passed', reason='Supported', evidence_ids=[wire[0]['id']], gap=None)
    valid = validate_verification({'results': [row]}, [criterion(0)], wire)
    assert resolve_citations(valid, aliases)[0]['evidence_ids'] == [original_id]
    for invalid in [original_id, 'tool:earlier-page-only', 'invented', '']:
        with pytest.raises(ValueError, match='registered evidence'):
            validate_verification({'results': [{**row, 'evidence_ids': [invalid]}]}, [criterion(0)], wire)
    assert evidence[0]['observations'][0]['evidence_ids'] == ['tool:earlier-page-only']  # Audit originals untouched.


def test_directory_only_reference_cannot_pass_an_obligation():
    row = dict(criterion_id='c0', status='passed', reason='Directory is complete', evidence_ids=['directory'], gap=None)
    with pytest.raises(ValueError, match='inspected evidence'):
        validate_verification({'results': [row]}, [criterion(0)], [{'id': 'directory', 'directory_only': True}])


@pytest.mark.asyncio
async def test_restored_passed_receipt_with_unregistered_reference_is_rejected(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview

    assembly, _, runtime = await setup(tmp_path, [criterion(0)])
    try:
        row = dict(criterion_id='c0', status='passed', reason='Unsupported citation', evidence_ids=['not-registered'], gap=None)
        ref = assembly.publish_artifact({'criteria': [criterion(0)], 'evidence': [{'id': 'candidate', 'content': 'report'}],
                                         'results': [row]}, 'verification_evidence')
        assembly.acceptance.data['results'] = [{**row, 'judgment_artifact': ref}]
        batch = BatchReview(assembly.acceptance, runtime, [], {}, assembly.acceptance.fingerprint('report'), None)
        with pytest.raises(AcceptanceBlocked, match='invalid criteria or evidence references'):
            await batch.run([criterion(0)])
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_cached_selector_cannot_turn_unregistered_ids_into_an_empty_view(tmp_path):
    from loom.tasks.acceptance_batches import BatchReview
    from loom.tasks.acceptance_selection import select_evidence

    assembly, _, runtime = await setup(tmp_path, [criterion(0)])
    for index in range(30):
        receipt(assembly, f'tool:{index}', f'cat source-{index}.txt', 'source ' * 200)
    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        return LlmResponse(json.dumps({'evidence_ids': ['tool:0'], 'reason': 'Source selected'}))

    assembly.acceptance_model_request = request
    try:
        batch = BatchReview(assembly.acceptance, runtime, [], {}, {}, None)
        view = batch.view([criterion(0)])
        assert any(r.get('directory_only') for r in view)
        await select_evidence(batch, [criterion(0)], view)
        saved = next(iter(batch.pending['selections'].values()))
        saved['artifact'] = assembly.publish_artifact({'selection': {'evidence_ids': ['unregistered'], 'reason': 'Invalid saved selection'}},
                                                       'verification_evidence_selection')
        with pytest.raises(AcceptanceBlocked, match='unregistered references'):
            await select_evidence(batch, [criterion(0)], view)
        assert calls == [1]
    finally:
        await assembly.close()
