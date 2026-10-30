import asyncio
import json
from copy import deepcopy

import pytest

from loom.service.skills import DEFAULTS, SessionSkills
from loom.skills.claims import materialize, method_size, projection
from loom.skills.contracts import canonical
from loom.skills.induction import BudgetStop, Induction
from loom.skills.lineage import assign, related
from loom.skills.retrieval import delivery, freeze_split, recall
from tests.skills.helpers import Judge, candidate, fixture, seed, semantic


def ready(tmp_path, *, count=3):
    base, store, sources, catalog = fixture(tmp_path)
    for index in range(count):
        seed(base, sources, tmp_path, index)
    catalog.reconcile(str(tmp_path))
    return base, store, sources, catalog


def job(store, cases, **settings):
    body = dict(id='job-test', skill_id='skill-test', workspace=cases[0]['scope']['workspace'], query='HTTP connection', model='test',
        config_digest='fixed', state='queued', settings={**DEFAULTS, **settings}, corpus=cases, split=freeze_split(cases),
        usage=dict(calls=0, tokens=0, seconds=0), checkpoints={}, requests=[], limitations=[], coverage={}, delivered_cases=[])
    with store.transaction() as db:
        db.execute('INSERT INTO skill_jobs VALUES(?,?,?,?)', (body['id'], body['workspace'], body['state'], canonical(body)))
    return body


def test_end_to_end_source_backed_candidate_never_self_promotes(tmp_path):
    _, store, _, catalog = ready(tmp_path)
    cases = store.all_cases(str(tmp_path))
    assert len({c['task_family_id'] for c in cases}) == 3
    judge = Judge()
    result = asyncio.run(Induction(store, catalog, judge).run(job(store, cases)))
    assert judge.calls == ['selection', 'alignment', 'synthesis', 'review-0']
    version = store.check_version(result['version_id'])
    assert version['sources_available']
    assert version['state'] == 'candidate' and version['evidence_state'] == 'proposed'
    assert version['contract']['composition_status'] == 'proposed_composition'
    claim = version['contract']['claims'][0]
    assert len(claim['supporting_family_ids']) == 3
    assert all(e['source']['session_id'] for e in claim['evidence_refs'])
    with pytest.raises(ValueError, match='executable validation'):
        store.transition(version['id'], 'active', version['revision'])
    # Frozen checkpoints are reused without extra provider work.
    asyncio.run(Induction(store, catalog, judge).run(result))
    assert len(judge.calls) == 4


def test_repeated_evaluations_and_uncertain_boundaries_do_not_add_support(tmp_path):
    base, store, sources, catalog = fixture(tmp_path)
    original = seed(base, sources, tmp_path, 0, uncertain=True)
    duplicate = deepcopy(original)
    duplicate['id'] = 'another-evaluation'
    with base.transaction() as db:
        db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', (duplicate['id'], duplicate['session_id'], 'copy', canonical(duplicate)))
    catalog.reconcile(str(tmp_path))
    cases = store.all_cases(str(tmp_path))
    assert len(cases) == 1 and len(cases[0]['evaluation_views']) == 2
    manifest = delivery(cases)['manifest']
    value = materialize(candidate(list(manifest)), manifest, cases)
    assert value['claims'][0]['supporting_family_ids'] == []


def test_failures_unknowns_and_all_catalog_pages_are_preserved(tmp_path):
    base, store, sources, catalog = fixture(tmp_path)
    seed(base, sources, tmp_path, 0, status='stale')
    catalog.reconcile(str(tmp_path))
    case = store.all_cases(str(tmp_path))[0]
    assert not case['evidence'] and 'usable_memory' in case['quality']['missing_fields']
    # Paging continues beyond the runtime-memory 500-record cap.
    cases = []
    for i in range(507):
        c = deepcopy(case)
        c['id'] = f'case:{i:04d}'
        cases.append(c)
    store.replace_session(case['session_id'], cases, 'fixture')
    assert len(store.all_cases(str(tmp_path))) == 507
    assert store.cases(str(tmp_path), limit=100)['next_cursor'] == 'case:0099'


def test_chinese_path_port_variants_and_transitive_lineage(tmp_path):
    _, store, _, _ = ready(tmp_path)
    left = store.all_cases(str(tmp_path))[0]
    right = deepcopy(left)
    right.update(id='new', session_id='other', source_digest='other')
    left['task_contract']['objective'] = '修复 /tmp/a 接口连接复用错误，端口 8080'
    right['task_contract']['objective'] = '修复 /tmp/b 接口连接复用错误，端口 9090'
    assert related(left, right) == 'normalized_objective'
    groups = assign([left, right])
    assert groups[left['id']]['task_family_id'] == groups[right['id']]['task_family_id']
    separate = deepcopy(right)
    separate.update(id='separate', session_id='third', source_digest='third')
    separate['task_contract']['objective'] = '分析产品增长趋势'
    assert related(left, separate) is None


def test_holdout_never_delivered_and_exposure_cannot_become_independent(tmp_path):
    _, store, _, catalog = ready(tmp_path)
    cases = store.all_cases(str(tmp_path))
    # Use synthetic disjoint lineage groups to exercise frozen split selection separately from text dedup.
    for i in range(3, 11):
        c = deepcopy(cases[i % 3])
        c.update(id=f'case-extra-{i}', task_family_id=f'family-{i}', split_group_id=f'group-{i}')
        cases.append(c)
    split = freeze_split(cases)
    assert set(split.values()) == {'discovery', 'validation', 'holdout'}
    found, _ = recall(cases, 'HTTP', split=split)
    assert all(split[c['split_group_id']] == 'discovery' for c in found)
    held = next(c for c in cases if split[c['split_group_id']] == 'holdout')
    assert freeze_split(cases, exposed_groups=[held['split_group_id']])[held['split_group_id']] == 'discovery'
    j = job(store, cases)
    with pytest.raises(ValueError, match='Held-out'):
        Induction(store, catalog, Judge()).expose(j, [held])


def test_budget_reservation_failed_resume_and_forged_handles(tmp_path):
    _, store, _, catalog = ready(tmp_path)
    cases = store.all_cases(str(tmp_path))
    judge = Judge(fail_stage='synthesis')
    body = job(store, cases)
    with pytest.raises(ValueError, match='interrupted'):
        asyncio.run(Induction(store, catalog, judge).run(body))
    saved = store.job(body['id'])
    assert saved['requests'][-1]['state'] == 'response_unknown'
    assert saved['usage']['tokens'] > 8000
    judge.fail_stage = None
    asyncio.run(Induction(store, catalog, judge).run(saved))
    assert judge.calls.count('alignment') == 1 and judge.calls.count('synthesis') == 2
    delivered = delivery(cases)
    with pytest.raises(ValueError, match='Undelivered'):
        materialize(candidate(['invented']), delivered['manifest'], cases)
    body = deepcopy(saved)
    body['settings']['max_calls'] = body['usage']['calls']
    with pytest.raises(BudgetStop):
        asyncio.run(Induction(store, catalog, judge).call(body, 'another', {'type': 'object'}, {}))


def test_source_mutation_suspends_version_and_revoked_evidence_cannot_export(tmp_path):
    base, store, sources, catalog = ready(tmp_path)
    result = asyncio.run(Induction(store, catalog, Judge()).run(job(store, store.all_cases(str(tmp_path)))))
    version = store.version(result['version_id'])
    store.transition(version['id'], 'trial', version['revision'])
    with base.transaction() as db:
        row = db.execute('SELECT id,body FROM semantic_jobs LIMIT 1').fetchone()
        changed = json.loads(row['body'])
        next(iter(changed['checkpoint']['memory']['records'].values()))['publication_state'] = 'stale'
        db.execute('UPDATE semantic_jobs SET body=? WHERE id=?', (canonical(changed), row['id']))
    checked = store.check_version(version['id'])
    assert checked['state'] == 'suspended' and not checked['sources_available']
    manager = SessionSkills(sources, semantic(Judge()))
    try:
        assert 'contract' not in manager.public_version(version['id'])
        with pytest.raises(ValueError, match='Unavailable'):
            manager.dispatch('GET', ['versions', version['id'], 'package'],
                             {'session_id': [changed['session_id']]}, None)
    finally:
        manager.close()


def test_service_job_identity_cas_and_no_cross_workspace(tmp_path):
    _, store, sources, _ = ready(tmp_path)
    manager = SessionSkills(sources, semantic(Judge()))
    try:
        sid = store.all_cases(str(tmp_path))[0]['session_id']
        first = manager.start(sid, dict(query='HTTP connection'))
        manager.futures[first['id']].result(timeout=10)
        again = manager.start(sid, dict(query='HTTP connection'))
        assert first['id'] == again['id'] and again['state'] == 'completed'
        version = manager.public_version(again['version_id'])
        store.transition(version['id'], 'suspended', version['revision'])
        with pytest.raises(ValueError, match='revision conflict'):
            store.transition(version['id'], 'retired', version['revision'])
        (tmp_path / 'other').mkdir()
        other = sources.store.create('other', dict(workspace=str(tmp_path / 'other')))['session_id']
        with pytest.raises(ValueError, match='outside'):
            manager.dispatch('GET', ['versions', version['id']], {'session_id': [other]}, None)
    finally:
        manager.close()


def test_real_holdout_content_is_absent_from_every_model_request(tmp_path):
    base, store, sources, catalog = fixture(tmp_path)
    goals = ['payment reconciliation proxy', 'healthcare request framing', 'geospatial cache routing',
             'video playback protocol negotiation', 'archive transfer chunk verification', 'distributed tracing propagation',
             'inventory event delivery semantics']
    for i, goal in enumerate(goals):
        seed(base, sources, tmp_path, i, objective='HTTP ' + goal)
    catalog.reconcile(str(tmp_path))
    body = job(store, store.all_cases(str(tmp_path)))
    assert list(body['split'].values()).count('validation') == 2
    assert list(body['split'].values()).count('holdout') == 2
    judge = Judge()
    asyncio.run(Induction(store, catalog, judge).run(body))
    prompt = json.dumps(judge.messages)
    for case in body['corpus']:
        if body['split'][case['split_group_id']] != 'discovery':
            assert case['id'] not in prompt
            assert case['task_contract']['objective'] not in prompt
            assert case['evidence'][0]['text'] not in prompt


def test_multiple_confirmed_goals_in_one_session_are_not_the_same_episode(tmp_path):
    _, store, _, _ = ready(tmp_path)
    left, right = deepcopy(store.all_cases(str(tmp_path))[:2])
    right['session_id'] = left['session_id']
    right['source_digest'] = left['source_digest']  # One session analysis may contain several independent runs.
    groups = assign([left, right])
    assert groups[left['id']]['task_family_id'] != groups[right['id']]['task_family_id']
    right['quality']['boundary_uncertain'] = True
    groups = assign([left, right])
    assert groups[left['id']]['task_family_id'] == groups[right['id']]['task_family_id']


def test_skill_induced_behavior_and_incomplete_quotes_cannot_inflate_discovery(tmp_path):
    base, store, sources, catalog = fixture(tmp_path)
    seed(base, sources, tmp_path, 0, exposure=True)
    catalog.reconcile(str(tmp_path))
    cases = store.all_cases(str(tmp_path))
    supplied = delivery(cases)
    c = materialize(candidate(list(supplied['manifest'])), supplied['manifest'], cases)
    assert not c['claims'][0]['supporting_family_ids']
    bounded = delivery(cases, max_chars=1)
    assert bounded['omitted_case_ids'] and not bounded['manifest']
    with pytest.raises(ValueError, match='Undelivered'):
        materialize(candidate(list(supplied['manifest'])), bounded['manifest'], cases)


def test_rejected_candidate_repairs_are_bounded_and_not_permanently_cached(tmp_path):
    _, store, _, catalog = ready(tmp_path)
    body = job(store, store.all_cases(str(tmp_path)))
    judge = Judge(forged=True)
    with pytest.raises(ValueError, match='Undelivered'):
        asyncio.run(Induction(store, catalog, judge).run(body))
    assert judge.calls == ['selection', 'alignment', 'synthesis', 'synthesis-repair-1', 'synthesis-repair-2']
    judge.forged = False
    saved = store.job(body['id'])
    asyncio.run(Induction(store, catalog, judge).run(saved))
    assert judge.calls.count('selection') == 1
    assert judge.calls.count('alignment') == 1
    assert judge.calls.count('synthesis-repair-2') == 2
    assert saved['state'] == 'completed'


def test_oversized_method_repairs_with_measured_feedback_and_reuses_completed_stages(tmp_path):
    from dataclasses import replace

    from loom.core import ok

    class VerboseJudge(Judge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            response = (await super().chat(messages, tools, cancellation, **kwargs)).unwrap()
            request = json.loads(messages[1].content)
            if request['stage'] == 'synthesis':
                draft = json.loads(response.content)
                draft.update(summary='详细说明' * 300, trigger='触发条件' * 300,
                             avoid=['保留重要边界' * 200] * 4)
                return ok(replace(response, content=canonical(draft)))
            if request['stage'] == 'synthesis-repair-1':
                feedback = request['input']
                assert feedback['method_size']['characters'] > 6000
                assert feedback['method_size']['fields']['avoid'] > 4000
                assert '5000' in feedback['repair_instructions']
                assert 'verification' in feedback['repair_instructions']
                assert 'cases' not in feedback and 'alignment' not in feedback
                assert 'claims' not in feedback['method']
                assert 'claims' not in request['schema']['properties']
                patch = json.loads(response.content)
                patch.pop('required_tools')
                patch.pop('guards')
                return ok(replace(response, content=canonical(patch)))
            return ok(response)

    _, store, _, catalog = ready(tmp_path)
    judge = VerboseJudge(fail_stage='synthesis-repair-1')
    body = job(store, store.all_cases(str(tmp_path)))
    with pytest.raises(ValueError, match='interrupted'):
        asyncio.run(Induction(store, catalog, judge).run(body))
    judge.fail_stage = None
    result = asyncio.run(Induction(store, catalog, judge).run(store.job(body['id'])))
    assert result['state'] == 'completed'
    assert judge.calls.count('selection') == judge.calls.count('alignment') == judge.calls.count('synthesis') == 1
    contract = store.version(result['version_id'])['contract']
    assert len(contract['claims'][0]['supporting_family_ids']) == 3
    assert contract['required_tools'] == ['shell_execute']
    assert contract['guards'] == [dict(field='runtime', value='native_os')]
    assert len(canonical(projection(contract))) == method_size(contract)['characters'] <= 6000


def test_runtime_composition_status_counts_toward_method_limit(tmp_path):
    _, store, _, _ = ready(tmp_path)
    cases = store.all_cases(str(tmp_path))
    supplied = delivery(cases)
    draft = candidate(list(supplied['manifest']))
    draft['gaps'] = ['x' * 1500] * 3
    remaining = 6000 - len(canonical(projection(draft))) - 3
    draft['gaps'].append('x' * remaining)
    assert len(canonical(projection(draft))) == 6000
    with pytest.raises(ValueError, match='maximum is 6000'):
        materialize(draft, supplied['manifest'], cases)


def test_oversize_does_not_mask_invalid_evidence_or_preserve_it_during_repair(tmp_path):
    from dataclasses import replace

    from loom.core import ok

    class InvalidJudge(Judge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            response = (await super().chat(messages, tools, cancellation, **kwargs)).unwrap()
            request = json.loads(messages[1].content)
            if request['stage'] == 'synthesis':
                draft = json.loads(response.content)
                draft['gaps'] = ['x' * 1500] * 4
                draft['claims'][0]['supports'] = ['CLM-S1']
                draft['claims'][0]['unknown'] = ['missing environment snapshot']
                return ok(replace(response, content=canonical(draft)))
            if request['stage'] == 'synthesis-repair-1':
                feedback = request['input']
                assert 'maximum is 6000' in feedback['host_feedback']
                assert 'CLM-S1' in feedback['host_feedback']
                assert 'missing environment snapshot' in feedback['host_feedback']
                assert feedback['allowed_evidence_handles']
                assert 'claims' in request['schema']['properties']
            return ok(response)

    _, store, _, catalog = ready(tmp_path)
    result = asyncio.run(Induction(store, catalog, InvalidJudge()).run(job(store, store.all_cases(str(tmp_path)))))
    assert result['state'] == 'completed'
