from copy import deepcopy

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.issues import issue
from loom.evaluation.memory.obligations import reconcile_framework_obligations
from loom.evaluation.work_items import retry_failed
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import run
from tests.evaluation.test_memory_anchors import AnchorJudge


def obsolete_candidate(unit_id, stage_id, action_id):
    cid = 'candidate:obsolete'
    error = issue('memory.key_action_membership', 'Key action is not attributed to this stage',
                  stage='memory', unit_id=unit_id, record_type='contributions', candidate_id=cid)
    return dict(id=cid, unit_id=unit_id, kind='contributions', issue=error, error=error['reason'],
                record=dict(key='finish', stage_id=stage_id, key_action_ids=[action_id], rating='key',
                            rationale='The framework action was mistakenly assigned to this stage.', evidence=[]))


def checkpoint():
    candidate = obsolete_candidate('window', 'stage', 'finish')
    unit = dict(id='window', mode='extract', complete=False, work_state='settled', disposition='quarantined',
                scope=dict(stages=[], actions=[dict(id='finish', memberships=[]),
                    dict(id='plan', memberships=[dict(stage_id='stage')])]),
                issues=[deepcopy(candidate['issue'])], errors=[candidate['error']], accepted_records=[],
                repair_targets=[candidate['id']], rejected_records=[deepcopy(candidate)], evidence=[{'text': 'original'}])
    return dict(extraction=dict(cards=[dict(id='finish', kind='tool', name='finish'),
                                      dict(id='plan', kind='boundary', name='plan.completed')]),
                stages={'stage': {'id': 'stage'}}, assessments={'finish': {'stage_ids': ['stage']}},
                usage={'calls': 74}, responses=[{'text': 'original response'}],
                memory=dict(units={'window': unit}, execution_complete=True, complete=False,
                            records={'accepted': {'unit_id': 'window', 'publication_state': 'published', 'kind': 'facts', 'summary': 'Keep me'}},
                            quarantine={candidate['id']: candidate}))


def test_framework_obligation_retirement_is_idempotent_and_never_accepts_the_failed_claim():
    cp = checkpoint()
    before = deepcopy(cp)
    assert reconcile_framework_obligations(cp) == ['candidate:obsolete']
    unit = cp['memory']['units']['window']
    assert not cp['memory']['quarantine'] and cp['memory']['complete']
    assert unit['complete'] and not unit['issues'] and not unit['repair_targets']
    audit = unit['retired_candidates'][0]
    assert audit['resolution'] == 'not_applicable' and audit['model_calls'] == 0
    assert audit['record'] == before['memory']['quarantine'][audit['id']]['record']
    assert cp['memory']['records'] == before['memory']['records']
    assert unit['evidence'] == before['memory']['units']['window']['evidence']
    assert cp['usage'] == before['usage'] and cp['responses'] == before['responses']
    after = deepcopy(cp)
    assert reconcile_framework_obligations(cp) == [] and cp == after


@pytest.mark.parametrize('case', ['real_member', 'missing_card', 'foreign_stage', 'current_stage', 'pending_response'])
def test_retirement_does_not_hide_unproven_or_still_required_obligations(case):
    cp = checkpoint()
    if case == 'real_member':
        cp['extraction']['cards'][0]['name'] = 'read_file'
    elif case == 'missing_card':
        cp['extraction']['cards'].pop()
    elif case == 'foreign_stage':
        cp['stages'].clear()
    elif case == 'current_stage':
        cp['memory']['units']['window']['scope']['stages'] = [{'id': 'stage'}]
    else:
        cp['memory']['units']['window']['pending_response'] = 'saved response'
    before = deepcopy(cp)
    assert reconcile_framework_obligations(cp) == [] and cp == before


def test_other_candidate_failures_and_unfinished_pipeline_survive_retirement():
    cp = checkpoint()
    other = dict(id='other', unit_id='window', kind='facts', record={},
                 issue=issue('memory.observation_required', 'Missing output', candidate_id='other'))
    cp['memory']['quarantine']['other'] = other
    cp['memory']['execution_complete'] = False
    assert reconcile_framework_obligations(cp) == ['candidate:obsolete']
    assert cp['memory']['quarantine'] == {'other': other}
    assert not cp['memory']['complete'] and not cp['memory']['execution_complete']
    assert cp['memory']['units']['window']['issues'] == [other['issue']]


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_retry', [False, True])
async def test_saved_framework_failure_resolves_before_any_new_model_request(explicit_retry):
    store = source([*action(1), {'type': 'run.completed', 'status': 'completed'}])
    facts, cp, judge = build_behavior_facts(store), {}, AnchorJudge()
    await run(store, facts, 'run', cp, judge, extract_memory=True, batch_rounds=2)
    boundary = next(c for c in cp['extraction']['cards'] if c['kind'] == 'boundary')
    stage = next(iter(cp['stages']))
    unit = next(u for u in cp['memory']['units'].values() if u['mode'] == 'extract'
                and [a['id'] for a in u['scope']['actions']] == [boundary['id']])
    cp['assessments'][boundary['id']]['stage_ids'] = [stage]
    unit['scope']['stages'] = [deepcopy(cp['stages'][stage])]
    candidate = obsolete_candidate(unit['id'], stage, boundary['id'])
    cp['memory']['quarantine'][candidate['id']] = candidate
    unit.update(complete=False, work_state='settled', disposition='quarantined', issues=[candidate['issue']],
                errors=[candidate['error']], repair_targets=[candidate['id']])
    cp['memory']['complete'] = False
    if explicit_retry:
        retry_failed(cp)
    accepted, usage = deepcopy(cp['memory']['records']), deepcopy(cp['usage'])
    requests = len(judge.requests), len(judge.memory_requests)
    result = await run(store, facts, 'run', cp, judge, extract_memory=True, batch_rounds=2)
    assert result['result_state'] == 'ready'
    assert cp['memory']['records'] == accepted and cp['usage'] == usage
    assert (len(judge.requests), len(judge.memory_requests)) == requests
    assert result['memory']['retired_candidates'][0]['resolution'] == 'not_applicable'
