"""Framework context must neither invent success nor create endless retry work."""

import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.evidence_delivery import prepare
from loom.evaluation.framework import card_policy
from loom.evaluation.issues import RuleViolation
from loom.evaluation.memory.contracts import protocol_for
from loom.evaluation.memory.records import commit_records
from loom.evaluation.memory.request import focus_repair, messages_for, repair_context
from loom.evaluation.memory.validation import validate_record
from loom.evaluation.memory.wire import materialize
from loom.evaluation.work_items import retry_failed, status
from loom.llm import LlmResponse
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import GraphJudge, run
from tests.evaluation.test_memory_anchors import AnchorJudge
from tests.evaluation.test_trace_memory import trace


@pytest.mark.asyncio
async def test_boundary_only_windows_are_recorded_without_model_calls_or_unknown_quality_gaps():
    store = source([*action(1), {'type': 'acceptance.gate.blocked', 'state': 'blocked'},
                    {'type': 'acceptance.budget.renewed', 'generation': 1},
                    {'type': 'verification.started', 'description': 'Checking'},
                    {'type': 'run.stopped', 'reason': 'Budget'}])
    cp, judge = {}, GraphJudge()
    result = await run(store, build_behavior_facts(store), 'run', cp, judge, batch_rounds=2)
    assert len(judge.requests) == 1
    assert result['unknown_judgments'] == 0
    assert result['result_state'] == 'ready'
    assert result['coverage']['recorded_boundary_cards'] >= 3
    boundaries = {c['id'] for c in cp['extraction']['cards'] if c['kind'] == 'boundary'}
    assert boundaries <= cp['assessments'].keys()
    assert all(cp['assessments'][cid]['contribution'] == 'unknown' for cid in boundaries)
    # Saved v11 windows used abstained solely because host records were unknown.
    unit = next(u for u in cp['units'].values() if set(u['cards']) <= boundaries)
    unit.update(disposition='abstained', complete=False)
    result = await run(store, build_behavior_facts(store), 'run', cp, judge, batch_rounds=2)
    assert result['result_state'] == 'ready'
    assert len(judge.requests) == 1
    # A real action with insufficient evidence still makes the result partial.
    real = next(c['id'] for c in cp['extraction']['cards'] if c['kind'] == 'tool')
    cp['assessments'][real]['status'] = 'unknown'
    assert status(cp)['unknown_judgments'] == 1
    assert status(cp)['result_state'] == 'partial'


@pytest.mark.parametrize('card,semantic,key', [
    ({'kind': 'boundary', 'name': 'acceptance.gate.passed'}, False, False),
    ({'kind': 'boundary', 'name': 'plan.updated'}, False, False),
    ({'kind': 'tool', 'name': 'finish'}, True, False),
    ({'kind': 'model', 'name': 'judge', 'actor': {'role': 'verification'}}, True, False),
    ({'kind': 'tool', 'name': 'shell_execute'}, True, True),
])
def test_framework_policy_preserves_real_calls_but_excludes_root_actions_from_stage_keys(card, semantic, key):
    policy = card_policy(card)
    assert policy['semantic_review'] is semantic
    assert policy['stage_key_action'] is key


def workflow_unit():
    unit = dict(id='route', mode='synthesis', scope=dict(goals=[dict(id='g', parent_goal_id=None)], stages=[],
        actions=[dict(id='a', kind='tool', memberships=[dict(goal_id='g', stage_id=None, basis='default_rule')])]),
        evidence=[dict(id=f'e{i}', action_id='a', text=f'Recorded result {i}', basis='observed_output', truncated=False,
                       ref=dict(source_sha256='source', line_number=i + 1, field_path='output', start=0, end=17)) for i in range(10)])
    prepare(unit)
    handles = list(unit['manifest']['handles'])
    row = dict(key='route', summary='Recorded route', confidence='medium', scope='This run', evidence=[],
               preconditions='Same host', steps=[dict(text=f'Step {i}', evidence=[h], basis='observed') for i, h in enumerate(handles)],
               avoid='Repeated probes', expected_benefit='Reuse recorded sequence', preserve='Checks', validation='observed')
    return unit, row


def test_workflow_preserves_evidence_for_all_ten_steps_instead_of_hitting_hidden_eight_span_limit():
    unit, row = workflow_unit()
    result, provenance = materialize('workflows', row, unit)
    accepted = validate_record('workflows', result, unit)
    assert len(accepted['citations']) == len(provenance['step_support']) == 10
    unit['evidence'][-1]['basis'] = 'observed_runtime_state'
    with pytest.raises(RuleViolation, match='Observed steps need recorded output'):
        materialize('workflows', row, unit)


def test_request_contract_limits_observation_labels_to_delivered_evidence_capabilities():
    unit, _ = workflow_unit()
    for entry in unit['evidence']:
        entry['basis'] = 'observed_runtime_state'
    schema = protocol_for(unit).schema['properties']
    assert schema['facts']['items']['properties']['basis']['enum'] == ['observed', 'inferred']
    assert schema['lessons']['items']['properties']['validation']['enum'] == ['proposed']
    assert schema['workflows']['items']['properties']['steps']['items']['properties']['basis']['enum'] == ['proposed']
    for entry in unit['evidence']:
        entry['basis'] = 'model_claim'
    schema = protocol_for(unit).schema['properties']
    assert schema['facts']['items']['properties']['basis']['enum'] == ['inferred']


@pytest.mark.asyncio
async def test_requested_page_survives_repair_focus_on_another_actions_rejected_candidate():
    class PageJudge(AnchorJudge):
        saw_page = False

        async def chat(self, messages, *args, **kwargs):
            response = await super().chat(messages, *args, **kwargs)
            payload = json.loads(messages[1].content)
            if payload.get('mode') != 'extract':
                return response
            rows = json.loads(response.value.content)
            if not payload.get('repair_targets'):
                model = next(a for a in payload['actions'] if a['kind'] == 'model')
                rows['facts'][0].update(evidence=[model['evidence'][0]['id']], basis='observed')
                rows['evidence_requests'] = [payload['evidence_pages'][0]['handle']]
            else:
                self.saw_page = any('MIDDLE FACT' in e['text'] for a in payload['actions'] for e in a['evidence'])
                assert self.saw_page
            return ok(LlmResponse(content=json.dumps(rows)))

    store = source([*action(1, output={'stdout': 'First result'}),
                    *action(2, output={'stdout': 'x' * 1300 + 'MIDDLE FACT' + 'z' * 3500})])
    cp, judge = {}, PageJudge()
    await run(store, build_behavior_facts(store), 'run', cp, judge, extract_memory=True)
    assert judge.saw_page


@pytest.mark.asyncio
async def test_explicit_retry_restores_candidates_after_an_empty_automatic_repair():
    class EmptyRepair(AnchorJudge):
        repair = False

        async def chat(self, messages, *args, **kwargs):
            payload = json.loads(messages[1].content)
            if payload.get('repair_targets') and not self.repair:
                return ok(LlmResponse(content=json.dumps(dict(contributions=[], facts=[], lessons=[], workflows=[], evidence_requests=[]))))
            if payload.get('repair_targets'):
                assert payload['rejected_records']
                assert payload['rejected_records'][0]['record']['key']
            return await super().chat(messages, *args, **kwargs)

    store, facts = trace()
    cp, judge = {}, EmptyRepair(bad=True)
    await run(store, facts, 'run', cp, judge, extract_memory=True)
    rejected = deepcopy(cp['memory']['quarantine'])
    assert rejected
    unit = cp['memory']['units'][next(iter(rejected.values()))['unit_id']]
    assert unit['rejected_records'] == []
    judge.bad, judge.repair = False, True
    retry_failed(cp)
    result = await run(store, facts, 'run', cp, judge, extract_memory=True)
    assert not cp['memory']['quarantine']
    assert result['result_state'] == 'ready'


def test_explicit_optional_withdrawal_is_audited_and_omission_does_not_resolve_quarantine():
    unit, row = workflow_unit()
    memory = dict(identity=dict(source='source', run_id='run', graph_revision=1), records={}, quarantine={})
    row['steps'][0]['evidence'] = ['missing']
    response = dict(contributions=[], facts=[], lessons=[], workflows=[row], evidence_requests=[])
    assert commit_records(memory, unit, json.dumps(response))
    candidate = next(iter(memory['quarantine']))
    unit['repair_targets'] = [candidate]
    unit['rejected_records'] = list(memory['quarantine'].values())
    schema = protocol_for(unit).schema
    response['workflows'] = []
    assert commit_records(memory, unit, json.dumps(response), schema=schema)
    response['withdrawals'] = [dict(candidate_id=candidate, reason='No evidence demonstrates the proposed route.')]
    assert not commit_records(memory, unit, json.dumps(response), schema=schema)
    assert not memory['quarantine'] and not memory['records']
    assert unit['withdrawn_candidates'][0]['record'] == row
    assert unit['withdrawn_candidates'][0]['withdrawal_reason']
    # A withdrawal cannot target an arbitrary candidate or an accepted record.
    assert commit_records(memory, unit, json.dumps(response), schema=schema)[0]['code'] == 'memory.withdrawal_scope'


def test_required_contributions_cannot_be_withdrawn():
    unit, _ = workflow_unit()
    unit.update(repair_targets=['required'], rejected_records=[dict(id='required', kind='contributions')])
    schema = protocol_for(unit).schema
    assert schema['properties']['withdrawals']['maxItems'] == 0
    memory = dict(identity=dict(source='source', run_id='run', graph_revision=1), records={}, quarantine={
        'required': dict(id='required', unit_id=unit['id'], kind='contributions', record={},
                         issue=dict(code='memory.stages_pending', category='coverage', reason='Required stage'))})
    response = dict(contributions=[], facts=[], lessons=[], workflows=[], evidence_requests=[],
                    withdrawals=[dict(candidate_id='required', reason='Cannot judge')])
    assert commit_records(memory, unit, json.dumps(response), schema=schema)
    assert 'required' in memory['quarantine']


def test_repair_feedback_is_bounded_without_mutating_oversized_or_malformed_candidates():
    unit, _ = workflow_unit()
    rows = {f'bad{i}': dict(id=f'bad{i}', kind='facts', unit_id=unit['id'],
        record={'key': f'bad{i}', 'summary': 'x' * 20000, 'evidence': [{}]}, error='Invalid shape') for i in range(3)}
    memory = dict(quarantine=rows)
    before = deepcopy(rows)
    repair_context(unit, memory)
    assert memory['quarantine'] == before
    assert len(json.dumps(unit['rejected_records'])) < 16000
    assert unit['rejected_records'][0]['record_omitted_chars'] > 0
    assert unit['omitted_rejected_records'] > 0
    # Invalid evidence IDs must remain a model validation error, not a host crash.
    unit['rejected_records'] = list(rows.values())
    focus_repair(unit)
    unit['rejected_records'] = [dict(kind='facts', record={'evidence': 1, 'action_ids': {}})]
    focus_repair(unit)


@pytest.mark.asyncio
async def test_memory_request_exposes_actual_stage_key_scope_and_observation_capabilities():
    store, facts = trace()
    cp = {}
    await run(store, facts, 'run', cp, AnchorJudge(), extract_memory=True)
    unit = deepcopy(next(u for u in cp['memory']['units'].values() if u['mode'] == 'extract'))
    unit['accepted_records'] = []
    payload = json.loads(messages_for(unit, 'text')[0][1].content)
    for stage in payload['stages']:
        assert stage['eligible_key_action_ids']
        assert all(any(m['stage_id'] == stage['id'] for m in a['memberships'])
                   for a in payload['actions'] if a['id'] in stage['eligible_key_action_ids'])
    assert payload['observation_handles']['tool_output']
