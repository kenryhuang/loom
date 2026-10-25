import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.evidence_delivery import prepare, resolve
from loom.evaluation.issues import EvaluationFailure, RuleViolation, request_budget_failure
from loom.evaluation.memory.contracts import protocol_for
from loom.evaluation.memory.lifecycle import dependencies, migrate
from loom.evaluation.memory.records import commit_records, replay_admission
from loom.evaluation.memory.wire import materialize
from loom.evaluation.work_items import retry_failed
from loom.llm import LlmResponse, TokenUsage
from tests.evaluation.test_incremental_graph import GraphJudge, run
from tests.evaluation.test_trace_memory import trace


@pytest.mark.parametrize('calls,remaining,codes', [
    (47, 1, []), (48, 2728, ['budget.calls']), (2, 0, ['budget.seconds']),
    (48, -1, ['budget.calls', 'budget.seconds']),
])
def test_request_budget_reports_only_exhausted_dimensions(calls, remaining, codes):
    failure = request_budget_failure(dict(calls=calls), dict(max_calls=48, max_seconds=3600), remaining,
                                     stage='memory', unit_id='memory:extract:test')
    assert ([e['code'] for e in failure.issues] if failure else []) == codes
    if failure:
        assert all(e['details']['used'] >= e['details']['limit'] for e in failure.issues)
        assert 'cumulative across resumes' in str(failure)


@pytest.mark.asyncio
async def test_memory_retry_pauses_at_shared_call_limit_and_resumes_without_losing_records():
    source, facts = trace()
    cp, judge = {}, AnchorJudge(bad=True)
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    accepted = deepcopy(cp['memory']['records'])
    retry_failed(cp)
    # Simulate a history whose earlier graph/repair calls consumed the ceiling.
    used = cp['usage']['calls'] = cp['resource_limits']['max_calls']
    requests = len(judge.memory_requests)
    with pytest.raises(EvaluationFailure) as error:
        await run(source, facts, 'run', cp, judge, extract_memory=True, max_calls=used)
    detail = error.value.issues[0]
    assert detail['code'] == 'budget.calls' and detail['stage'] == 'memory'
    assert detail['details'] == dict(setting='max_calls', used=used, limit=used)
    assert len(judge.memory_requests) == requests
    assert cp['memory']['records'] == accepted
    judge.bad = False
    done = await run(source, facts, 'run', cp, judge, extract_memory=True, max_calls=used + 10)
    assert done['execution_state'] == 'completed'
    assert all(cp['memory']['records'][k] == v for k, v in accepted.items())


class AnchorJudge(GraphJudge):
    def __init__(self, *, bad=False):
        super().__init__()
        self.memory_requests = []
        self.bad = bad

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        payload = json.loads(messages[1].content)
        if payload.get('mode') not in {'extract', 'synthesis'}:
            return await super().chat(messages, tools, cancellation, **kwargs)
        self.memory_requests.append(payload)
        evidence = [{**e, 'action_id': a['id']} for a in payload['actions'] for e in a['evidence']]
        rows = dict(contributions=[], facts=[], lessons=[], workflows=[], evidence_requests=[])
        if payload['mode'] == 'extract':
            for stage in payload['stages']:
                related = {a['id'] for a in payload['actions'] if any(m['stage_id'] == stage['id'] for m in a['memberships'])}
                ref = next(e for e in evidence if e['action_id'] in related)
                rows['contributions'].append(dict(key=stage['id'], stage_id=stage['id'], rating='supporting',
                    rationale='Recorded investigation', key_action_ids=[], evidence=[ref['id']]))
            observed = next((e for e in evidence if e['basis'] == 'observed_output'), next(iter(evidence), None))
            if observed:
                rows['facts'].append(dict(key='host', summary='Recorded trace evidence', scope='This trace only', confidence='low',
                    category='environment', basis='observed' if observed['basis'] == 'observed_output' else 'inferred', revalidate_when='Before use',
                    evidence=['A999.E1.S1'] if self.bad else [observed['id']]))
        return ok(LlmResponse(content=json.dumps(rows), usage=TokenUsage(20, 10, 30)))


@pytest.mark.asyncio
async def test_failed_reassessment_retains_unknown_until_valid_replacement_and_resolves_old_candidate():
    class Rejudge(AnchorJudge):
        phase = 'unknown'

        async def chat(self, messages, *args, **kwargs):
            answer = await super().chat(messages, *args, **kwargs)
            if json.loads(messages[1].content).get('mode') == 'extract':
                rows = json.loads(answer.value.content)
                for row in rows['contributions']:
                    row['key'] = self.phase + row['stage_id']
                    if self.phase == 'unknown':
                        row.update(rating='unknown', evidence=[])
                    elif self.phase == 'invalid':
                        row.update(rating='key', evidence=['a natural language description, not a delivered handle'])
                return ok(LlmResponse(content=json.dumps(rows)))
            return answer

    source, facts = trace()
    cp, judge = {}, Rejudge()
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    original = deepcopy(cp['memory']['records'])
    retry_failed(cp)
    assert cp['memory']['records'] == original
    judge.phase = 'invalid'
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert all(cp['memory']['records'][k] == v for k, v in original.items())
    assert cp['memory']['quarantine']
    issues = [e for u in cp['memory']['units'].values() for e in u.get('issues', [])]
    assert any(e['code'] == 'memory.anchor_undelivered' for e in issues)
    assert not any(e['code'] == 'memory.stages_pending' for e in issues)
    retry_failed(cp)
    judge.phase = 'corrected'
    done = await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert done['result_state'] == 'ready'
    assert not cp['memory']['quarantine']
    for record in cp['memory']['records'].values():
        if record['kind'] == 'contributions':
            assert record['rating'] == 'supporting'
            assert record['history'][-1]['rating'] == 'unknown'


@pytest.mark.asyncio
async def test_anchor_contract_enumerates_only_current_delivery_and_legacy_retry_restore_checks_dependencies():
    from jsonschema import Draft202012Validator

    from loom.evaluation.memory.lifecycle import restore_retry_unknowns

    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, AnchorJudge(), extract_memory=True)
    record = next(r for r in cp['memory']['records'].values() if r['kind'] == 'contributions')
    unit = cp['memory']['units'][record['unit_id']]
    unit['delivery_span_ids'] = [unit['evidence'][0]['id']]
    prepare(unit)
    schema = protocol_for(unit).schema
    Draft202012Validator.check_schema(schema)
    evidence = {**schema['properties']['facts']['items']['properties']['evidence'], '$defs': schema['$defs']}
    validator = Draft202012Validator(evidence)
    assert validator.is_valid(list(unit['manifest']['handles']))
    assert not validator.is_valid(['a prose description'])
    assert not validator.is_valid(['A999.E1.S1'])
    # v1 Retry incorrectly made a still-valid unknown stale. Restore only that case.
    unit['retry_generation'] = 1
    record.update(rating='unknown', citations=[], evidence_refs=[], publication_state='stale')
    cp['memory'].pop('retry_policy_version', None)
    restore_retry_unknowns(cp)
    assert record['publication_state'] == 'published'
    assert cp['memory']['retry_migrations'][-1]['record_id'] == record['id']
    record['publication_state'] = 'stale'
    record['dependencies']['stage'] = 'different-stage-identity'
    cp['memory'].pop('retry_policy_version', None)
    restore_retry_unknowns(cp)
    assert record['publication_state'] == 'stale'


@pytest.mark.asyncio
async def test_published_contribution_echo_is_idempotent_before_pending_capacity_check():
    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, AnchorJudge(), extract_memory=True)
    memory = cp['memory']
    record = next(r for r in memory['records'].values() if r['kind'] == 'contributions')
    unit = memory['units'][record['unit_id']]
    original = deepcopy(memory['records'])
    schema = protocol_for(unit).schema
    assert schema['properties']['contributions']['maxItems'] == 0
    # Same stable stage, different local key and proposed content: never overwrite.
    echo = dict(stage_id=record['stage_id'], key='new-local-key', rating='hindering')
    rows = dict(contributions=[echo], facts=[], lessons=[], workflows=[], evidence_requests=[])
    raw = json.dumps(rows)
    assert not commit_records(memory, unit, raw, schema=schema)
    assert memory['records'] == original
    assert unit['normalizations'][-1]['rule'] == 'published_contribution_echo_ignored'
    rows['contributions'].append(dict(stage_id='undelivered-stage', key='new'))
    errors = commit_records(memory, unit, json.dumps(rows), schema=schema)
    assert errors[0]['code'] == 'schema.maxItems'
    assert memory['records'] == original
    # Replay only the saved delivery; preserve raw response and original errors.
    unit['issues'] = [dict(code='schema.maxItems', record_type='contributions')]
    response = dict(batch_id=unit['id'], activity='memory', text=raw, response_schema=schema,
                    manifest=deepcopy(unit['manifest']), validation_version='memory-validation.6',
                    validation_issues=deepcopy(unit['issues']))
    cp['responses'].append(response)
    replay_admission(cp)
    assert not unit['issues']
    assert response['text'] == raw and response['validation_issues']
    assert response['revalidations'][-1]['model_calls'] == 0
    assert memory['records'] == original
    snapshot = deepcopy(cp)
    replay_admission(cp)
    assert cp == snapshot
    # Stale judgments are eligible for a new assessment; never suppress them.
    memory['records'][record['id']]['publication_state'] = 'stale'
    assert commit_records(memory, unit, raw, schema=schema)[0]['code'] == 'schema.maxItems'


@pytest.mark.asyncio
async def test_anchor_pipeline_materializes_citations_and_publishes_only_valid_records():
    source, facts = trace()
    cp, judge = {}, AnchorJudge()
    result = await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert result['execution_state'] == 'completed'
    assert result['result_state'] == 'ready'
    assert result['memory']['facts']
    for record in cp['memory']['records'].values():
        assert record['publication_state'] == 'published'
        assert record['provenance']['manifest_id']
        for ref, citation in zip(record['evidence_refs'], record['citations'], strict=True):
            assert source.resolve(ref) == citation['quote']
    before = deepcopy(cp['memory'])
    count = len(judge.memory_requests)
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert len(judge.memory_requests) == count
    assert cp['memory'] == before


@pytest.mark.asyncio
async def test_bad_fact_is_quarantined_without_blocking_later_windows_or_repeating_on_resume():
    source, facts = trace()
    cp, judge = {}, AnchorJudge(bad=True)
    result = await run(source, facts, 'run', cp, judge, extract_memory=True, batch_rounds=2)
    assert result['execution_state'] == 'completed' and result['result_state'] == 'partial'
    assert cp['memory']['cursor'] == len(cp['extraction']['cards'])
    assert cp['memory']['quarantine'] and not result['memory']['facts']
    assert result['memory']['coverage']['processed_actions'] == len(cp['extraction']['cards'])
    before = len(judge.memory_requests)
    await run(source, facts, 'run', cp, judge, extract_memory=True, batch_rounds=2)
    assert len(judge.memory_requests) == before


def test_manifest_requires_delivered_spans_and_unchanged_content():
    unit = dict(id='window', scope=dict(actions=[dict(id='a')]), evidence=[dict(id='old', action_id='a', text='first\nsecond\n',
        ref=dict(source_sha256='source', line_number=1, field_path='stdout', start=10, end=23), basis='observed_output', truncated=False)])
    prepare(unit)
    handle = next(iter(unit['manifest']['handles']))
    assert resolve(unit, [handle])[0]['text'] == 'first\nsecond\n'
    with pytest.raises(RuleViolation, match='not delivered'):
        resolve(unit, ['unseen'])
    unit['evidence'][0]['text'] = 'changed'
    with pytest.raises(RuleViolation, match='content changed'):
        resolve(unit, [handle])


@pytest.mark.asyncio
async def test_explicit_retry_repairs_only_quarantine_and_preserves_accepted_records():
    source, facts = trace()
    cp, judge = {}, AnchorJudge(bad=True)
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    accepted = deepcopy(cp['memory']['records'])
    judge.bad = False
    retry_failed(cp)
    result = await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert result['result_state'] == 'ready'
    assert not cp['memory']['quarantine']
    assert all(cp['memory']['records'][key] == value for key, value in accepted.items())


@pytest.mark.asyncio
async def test_unknown_is_terminal_but_does_not_mean_success():
    class Unknown(AnchorJudge):
        async def chat(self, messages, *args, **kwargs):
            answer = await super().chat(messages, *args, **kwargs)
            if json.loads(messages[1].content).get('mode') == 'extract':
                rows = json.loads(answer.value.content)
                for row in rows['contributions']:
                    row.update(rating='unknown', evidence=[])
                return ok(LlmResponse(content=json.dumps(rows)))
            return answer

    source, facts = trace()
    cp, judge = {}, Unknown()
    result = await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert result['execution_state'] == 'completed' and result['result_state'] == 'partial'
    assert result['memory']['coverage']['unknown_contributions'] > 0
    assert not cp['memory']['quarantine']
    count = len(judge.memory_requests)
    await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert len(judge.memory_requests) == count


@pytest.mark.asyncio
async def test_workflow_observed_requires_each_step_to_have_observed_support():
    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, AnchorJudge(), extract_memory=True)
    unit = next(u for u in cp['memory']['units'].values() if u['mode'] == 'synthesis')
    anchor = next(iter(unit['manifest']['handles']))
    row = dict(key='route', summary='Route', confidence='low', scope='Recorded run', evidence=[anchor],
               preconditions='Same host', steps=[dict(text='Unobserved improvement', evidence=[], basis='proposed')],
               avoid='Repeated failures', expected_benefit='Fewer calls', preserve='Tests', validation='observed')
    with pytest.raises(RuleViolation, match='every step'):
        materialize('workflows', row, unit)
    row['validation'] = 'proposed'
    result, provenance = materialize('workflows', row, unit)
    assert result['validation'] == 'proposed' and provenance['step_support'][0]['basis'] == 'proposed'


@pytest.mark.asyncio
async def test_unrelated_graph_revision_preserves_memory_but_changed_membership_invalidates_contribution():
    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, AnchorJudge(), extract_memory=True)
    original = deepcopy(cp['memory']['records'])
    cp['graph_revision'] += 1
    expected = {**cp['memory']['identity'], 'graph_revision': cp['graph_revision']}
    migrate(cp, expected)
    assert cp['memory']['records'] == original
    contribution = next(r for r in original.values() if r['kind'] == 'contributions')
    cid = contribution['slice_action_ids'][0]
    for link in cp['action_attributions']:
        if link['card_id'] == cid:
            link['stage_id'] = None
    migrate(cp, expected)
    assert cp['memory']['records'][contribution['id']]['publication_state'] == 'stale'
    assert all(r['publication_state'] == 'published' for r in cp['memory']['records'].values() if r['kind'] == 'facts')
    assert not cp['memory']['execution_complete']


@pytest.mark.asyncio
async def test_workflow_becomes_stale_when_supporting_record_changes():
    from tests.evaluation.test_trace_memory import MemoryJudge

    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, MemoryJudge(), extract_memory=True)
    workflow = next(r for r in cp['memory']['records'].values() if r['kind'] == 'workflows')
    source_id = workflow['source_record_ids'][0]
    cp['memory']['records'][source_id]['publication_state'] = 'stale'
    assert workflow['dependencies'] != dependencies(workflow, cp)
    migrate(cp, cp['memory']['identity'])
    assert workflow['publication_state'] == 'stale'


@pytest.mark.asyncio
async def test_evidence_page_reads_only_fixed_trace_and_keeps_exact_offsets():
    from loom.evaluation.behavior_facts import build_behavior_facts
    from loom.evaluation.evidence_delivery import page_catalog, read_page
    from tests.evaluation.test_behavior_sequence import action, source

    store = source(action(1, output={'stdout': 'start\n' + 'x' * 1600 + '\nMIDDLE FACT\n' + 'x' * 3000 + '\nend'}))
    cp = {}
    await run(store, build_behavior_facts(store), 'run', cp, AnchorJudge(), extract_memory=True)
    unit = next(u for u in cp['memory']['units'].values() if u['mode'] == 'extract')
    unit['pages'] = page_catalog(unit, store)
    found = []
    for handle in unit['pages']:
        for span in read_page(unit, handle, store):
            assert store.resolve(span['ref']) == span['text']
            if 'MIDDLE FACT' in span['text']:
                found.append(span)
    assert found
    with pytest.raises(RuleViolation, match='outside'):
        read_page(unit, '/etc/passwd', store)


def test_repair_reservation_survives_budget_pause_without_spending_twice():
    from loom.evaluation.issues import issue
    from loom.evaluation.repair import reserve_repair

    cp, unit = dict(usage=dict(calls=1)), dict(id='u')
    errors = [issue('memory.anchor_undelivered', 'Missing')]
    assert reserve_repair(cp, unit, errors, dict(max_calls=10))
    assert reserve_repair(cp, unit, errors, dict(max_calls=10))
    assert cp['repair_usage']['calls'] == 1
    unit.pop('repair_reserved')
    assert not reserve_repair(cp, unit, errors, dict(max_calls=10))


@pytest.mark.asyncio
async def test_memory_can_extract_facts_while_graph_structure_is_unresolved():
    class BrokenGraph(AnchorJudge):
        async def chat(self, messages, *args, **kwargs):
            if json.loads(messages[1].content).get('mode') not in {'extract', 'synthesis'}:
                return ok(LlmResponse(content='not valid JSON'))
            return await super().chat(messages, *args, **kwargs)

    source, facts = trace()
    cp, judge = {}, BrokenGraph()
    result = await run(source, facts, 'run', cp, judge, extract_memory=True)
    assert result['coverage']['graph_complete'] is False
    assert result['memory']['facts']
    assert result['execution_state'] == 'completed' and result['result_state'] == 'partial'
    assert cp['memory']['cursor'] == len(cp['extraction']['cards'])


def test_graph_reuse_is_scoped_and_does_not_redeclare_or_mutate_identity():
    from loom.evaluation.goal_loops.graph_updates import commit_updates
    from tests.evaluation.test_graph_updates import commit, fixture

    _, _, cp, extraction = fixture()
    card = extraction['cards'][0]
    root = cp['entities']['goals'][0]['id']
    assert not commit(cp, extraction, [('stages', dict(key='s', goal_id=root, title='Check', kind='verification', refs=card['refs'][:1]))])[0]
    sid = next(iter(cp['stages']))
    before = deepcopy(cp['stages'])
    args = dict(batch_id='later', cards=extraction['cards'], delivered=extraction['registry'], cutoff=100, target_ids={card['id']})
    row = ('reuse', dict(key='existing', kind='stage', target_id=sid))
    errors, _ = commit_updates(cp, [row], visible_ids=set(), **args)
    assert errors[0]['code'] == 'graph.reuse_scope'
    assert not commit_updates(cp, [row], visible_ids={sid}, **args)[0]
    assert cp['stages'] == before and cp['record_aliases']['later']['existing'] == sid


@pytest.mark.asyncio
async def test_migration_audit_is_read_only_and_never_claims_semantic_verification():
    from loom.evaluation.replay_audit import audit

    source, facts = trace()
    cp = {}
    await run(source, facts, 'run', cp, AnchorJudge(bad=True), extract_memory=True)
    job = dict(id='saved', session_id='s', model='test', checkpoint=cp, usage=cp['usage'])
    before = deepcopy(job)
    report = audit(job)
    assert job == before
    assert report['new_model_calls'] == 0 and report['usage_unchanged']
    assert report['previous_records'] == report['preserved_claims']
    assert report['semantic_quality'] == 'not_assessed_by_mechanical_replay'


@pytest.mark.asyncio
async def test_explicit_retry_can_reassess_unknown_graph_judgments():
    class Uncertain(GraphJudge):
        uncertain = True

        async def chat(self, *args, **kwargs):
            answer = await super().chat(*args, **kwargs)
            if self.uncertain:
                value = json.loads(answer.value.content)
                for group in value['groups']:
                    for row in group['assessments']:
                        row.update(status='unknown', contribution='unknown', necessity='unknown', purpose='Not established')
                return ok(LlmResponse(content=json.dumps(value)))
            return answer

    source, facts = trace()
    cp, judge = {}, Uncertain()
    first = await run(source, facts, 'run', cp, judge)
    assert first['result_state'] == 'partial'
    judge.uncertain = False
    retry_failed(cp)
    final = await run(source, facts, 'run', cp, judge)
    assert not judge.requests[-1]['accepted_card_ids']
    assert final['result_state'] == 'ready'
    assert all(row['purpose'] != 'Not established' for row in cp['assessments'].values())
