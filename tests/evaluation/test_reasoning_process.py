"""Public evidence endpoints, request ownership, partial review and source identity."""

import copy
from dataclasses import asdict

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import _validate_focus, partial_semantic
from loom.evaluation.evidence_review import EvidenceReview
from loom.evaluation.reasoning_activity import classify
from loom.evaluation.reasoning_metrics import build_reasoning_process, cost_index
from loom.evaluation.reasoning_process import reasoning_context, validate_transitions
from tests.evaluation.test_behavior_sequence import action, source


def fixture():
    events = [*action(1), *action(2), *action(3)]
    for n, event in enumerate(events):
        event['at'] = f'2026-10-11T00:00:{n:02d}+00:00'
    store = source(events)
    return store, build_behavior_facts(store)


def transition(store, facts, index=1, **extra):
    review = EvidenceReview(store)
    call = facts['model_calls'][index]
    tool = facts['reasoning_activity']['acquisitions'][index - 1]
    assertion = review.present(call['response_ref'], max_chars=10000)['evidence_id']
    result = review.present(tool['result_ref'], max_chars=10000)['evidence_id']
    row = dict(segment_id=facts['segments'][index]['id'], question_kind='diagnostic', claim_type='cause',
               label='The configuration causes the failure', status='supported', assertion_ref=assertion,
               evidence_refs=[result], observation_ids=[tool['id']], action_ids=[], evidence_use='adopted', **extra)
    return review, row


def test_base_classifies_reads_without_interpreting_opaque_shell_or_deduplicating_repeats():
    facts = build_behavior_facts(source([*action(1), *action(2), *action(3, 'shell_execute', {'command': 'cat a && node check.js'})]))
    activity = facts['reasoning_activity']
    assert [r['classification'] for r in activity['acquisitions']] == ['read', 'read', 'unknown']
    assert activity['classification_coverage'] == dict(total=3, known=2, by_class={'read': 2, 'unknown': 1}, basis='solver_physical_calls')
    assert activity['acquisitions'][1]['repeat_fact_ids']
    process = build_reasoning_process(facts)
    scope = process['scopes'][0]
    assert scope['costs_by_role']['solver']['reads'] == 2
    assert process['metrics'][scope['id']]['evidence']['mean'] is None
    assert process['questions'][0]['endpoint']['status'] == 'not_reviewed'
    assert process['windows'][0]['cost']['unknown_calls'] == 1


@pytest.mark.parametrize('command,expected', [('cat a b', 'read'), ('cat "a b"', 'read'), ('cat -', 'unknown'),
                                            ('cat a; echo x', 'unknown'), ('cat $(whoami)', 'unknown'), ('cat a\nrm b', 'unknown')])
def test_shell_classification_is_allowlisted(command, expected):
    assert classify('shell_execute', {'command': command})[0] == expected


def test_mixed_metadata_counts_one_physical_call_with_read_suboperations():
    kind, basis, count, reads = classify('custom', {}, {'operations': [{'kind': 'read'}, {'kind': 'read'}, {'kind': 'execute'}]})
    assert (kind, basis, count, reads) == ('mixed', 'recorded_operations', 3, True)


def test_assertion_time_is_not_the_time_of_its_earlier_supporting_quote():
    store, facts = fixture()
    review, row = transition(store, facts)
    primary = facts['segments'][1]
    context, _ = reasoning_context(facts, primary)
    accepted, issues = validate_transitions({'reasoning_transitions': [row]}, review, [primary['id']], facts, context)
    assert not issues
    assert accepted[0]['line'] == 6  # response, not tool output at line 4
    process = build_reasoning_process(facts, {'reasoning_transitions': accepted})
    endpoint = process['questions'][0]['endpoint']
    assert endpoint['cost']['model_calls'] == 2
    assert endpoint['cost']['reads'] == 1
    assert process['metrics'][process['scopes'][0]['id']]['evidence']['mean'] == 1
    assert not process['questions'][0]['adoption_lags']  # no recorded injection identity
    assert accepted[0]['visibility_status'] == 'inferred'
    bad = {**row, 'assertion_ref': row['evidence_refs'][0]}
    assert not validate_transitions({'reasoning_transitions': [bad]}, review, [primary['id']], facts, context)[0]


def test_future_results_and_cross_goal_evidence_do_not_create_early_progress():
    store, facts = fixture()
    review, row = transition(store, facts)
    later = facts['reasoning_activity']['acquisitions'][2]
    row.update(observation_ids=[later['id']], evidence_refs=[review.present(later['result_ref'], max_chars=10000)['evidence_id']])
    assert not validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)[0]
    review, row = transition(store, facts)
    facts['reasoning_activity']['acquisitions'][0]['goal_revision_id'] = 'other-goal'
    assert not validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)[0]


def test_retrospective_support_does_not_close_an_adoption_window():
    store, facts = fixture()
    review, row = transition(store, facts)
    row['evidence_use'] = 'retrospective'
    accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    process = build_reasoning_process(facts, {'reasoning_transitions': accepted})
    assert process['metrics'][process['scopes'][0]['id']]['evidence']['n'] == 0
    assert process['questions'][0]['endpoint']['cost'] is None
    assert process['occurrences']


def test_invalid_optional_transition_does_not_fail_existing_focus_judgments():
    store, facts = fixture()
    review, row = transition(store, facts)
    value = _validate_focus({'reasoning_transitions': [{**row, 'status': 'fiction'}, row]}, review, [row['segment_id']], facts)
    assert len(value['reasoning_transitions']) == 1
    assert len(value['reasoning_diagnostics']) == 1
    assert len(value['assessments']) == 5


def test_research_conclusion_and_unknown_are_not_invented_root_causes():
    store, facts = fixture()
    review, row = transition(store, facts)
    row.update(question_kind='research', claim_type='conclusion')
    accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    endpoint = build_reasoning_process(facts, {'reasoning_transitions': accepted})['questions'][0]['endpoint']
    assert endpoint['kind'] == 'conclusion'
    assert endpoint['root_cause_applicability'] == 'not_applicable'
    row['claim_type'] = 'cause'
    assert not validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)[0]


def test_request_owned_windows_partition_reads_and_tokens_and_keep_censored_tail():
    store, facts = fixture()
    rows = []
    for index in (1, 2):
        review, row = transition(store, facts, index)
        accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
        rows += accepted
    facts['episodes'][0]['execution_status'] = 'completed'
    facts['token_ledger'][1]['total_tokens'] = None
    process = build_reasoning_process(facts, {'reasoning_transitions': rows})
    windows = [w for w in process['windows'] if w['series'] == 'evidence']
    assert [w['cost']['reads'] for w in windows] == [1, 1, 1]
    assert [w['cost']['model_calls'] for w in windows] == [2, 1, 0]
    assert sum(w['cost']['total_tokens'] for w in windows) == 30
    assert sum(w['cost']['missing_total_tokens'] for w in windows) == 1
    assert windows[-1]['status'] == 'censored'
    metric = process['metrics'][process['scopes'][0]['id']]['evidence']
    assert (metric['n'], metric['mean'], metric['open_reads']) == (2, 1, 1)


def test_roles_and_goal_revisions_do_not_leak_into_solver_windows():
    events = [*action(1), *action(2)]
    events[4]['metadata'] = {'usage_role': 'verification'}
    events += [{'type': 'task.goal.revised', 'objective': 'New goal', 'input_cursor': 1}, *action(3)]
    facts = build_behavior_facts(source(events))
    p = build_reasoning_process(facts)
    assert len(p['scopes']) == 2
    assert p['scopes'][0]['costs_by_role']['solver']['model_calls'] == 1
    assert p['scopes'][0]['costs_by_role']['verification']['model_calls'] == 1
    assert p['scopes'][1]['costs_by_role']['solver']['reads'] == 1


def test_cross_segment_claim_identity_refutation_and_partial_checkpoint_survive():
    store, facts = fixture()
    review, first = transition(store, facts)
    rows, _ = validate_transitions({'reasoning_transitions': [first]}, review, [first['segment_id']], facts)
    context, anchors = reasoning_context(facts, facts['segments'][2], rows)
    review2, second = transition(store, facts, 2)
    second.update(status='refuted', claim_id=rows[0]['claim_id'])
    second['evidence_refs'].append(review2.present(rows[0]['assertion_ref'], max_chars=10000)['evidence_id'])
    later, issues = validate_transitions({'reasoning_transitions': [second]}, review2, [second['segment_id']], facts, context)
    assert not issues
    assert later[0]['claim_id'] == rows[0]['claim_id']
    assert later[0]['line'] > rows[0]['line']
    cp = dict(stages={f'focus:{first["segment_id"]}': {'result': {'reasoning_transitions': rows}},
                      f'focus:{second["segment_id"]}': {'result': {'reasoning_transitions': later}}})
    result = partial_semantic(facts, cp)
    endpoint = result['reasoning_process']['questions'][0]['endpoint']
    assert endpoint['disposition'] == 'refuted'
    assert endpoint['unrefuted_at_snapshot_id'] is None
    assert anchors


def test_strict_adoption_lag_requires_exact_identity_and_counts_intervening_requests():
    store, facts = fixture()
    tool = facts['reasoning_activity']['acquisitions'][0]
    tool['injections'] = [dict(ref=asdict(store.ref(store.events[4])), source_identity='tool_call_id', raw_output_preserved=True)]
    review, row = transition(store, facts, 2)
    row.update(observation_ids=[tool['id']], evidence_refs=[review.present(tool['result_ref'], max_chars=10000)['evidence_id']])
    accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    p = build_reasoning_process(facts, {'reasoning_transitions': accepted})
    assert p['questions'][0]['adoption_lags'][0]['cost']['model_calls'] == 1
    tool['injections'][0]['source_identity'] = 'content_match_only'
    accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    assert accepted[0]['visible_at'] is None


def test_parallel_returns_stay_in_the_request_window_and_duplicate_endpoints_coalesce():
    store, facts = fixture()
    review, row = transition(store, facts)
    accepted, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    second = copy.deepcopy(accepted[0])
    second.update(id='another-claim', claim_id='another-claim', label='Another observation')
    facts['reasoning_activity']['acquisitions'][0]['result_line'] = 12  # accounting never moves request ownership
    p = build_reasoning_process(facts, {'reasoning_transitions': accepted + [second]})
    assert p['metrics'][p['scopes'][0]['id']]['evidence']['n'] == 1
    assert p['windows'][0]['cost']['reads'] == 1
    count, _ = cost_index(facts)
    scope = facts['model_calls'][0]['episode_id'], facts['model_calls'][0]['goal_revision_id']
    assert count(scope, 4, 12, exclusive=True)['reads'] == 2


def test_imported_evidence_requires_current_delivery_and_does_not_copy_old_read_cost():
    events = [*action(1), {'type': 'task.goal.revised', 'objective': 'Reuse prior evidence', 'input_cursor': 1}, *action(2)]
    store = source(events)
    facts = build_behavior_facts(store)
    old = facts['reasoning_activity']['acquisitions'][0]
    current = facts['model_calls'][1]
    delivery = asdict(store.ref(store.events[5]))
    old['injections'] = [dict(ref=delivery, round_id=current['id'], source_identity='tool_call_id', raw_output_preserved=True)]
    review, row = transition(store, facts)
    row['imported_observation_ids'] = [old['id']]
    row['evidence_refs'].append(review.present(delivery, max_chars=10000)['evidence_id'])
    context, _ = reasoning_context(facts, facts['segments'][1])
    accepted, issues = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts, context)
    assert not issues
    p = build_reasoning_process(facts, {'reasoning_transitions': accepted})
    endpoint = next(q['endpoint'] for q in p['questions'] if q['id'] == current['goal_revision_id'])
    assert endpoint['cost']['reads'] == 0
    assert endpoint['cost']['model_calls'] == 1
    assert any(n['kind'] == 'imported_observation' for n in p['nodes'])
    old['injections'][0]['source_identity'] = 'content_match_only'
    assert not validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)[0]


def test_parallel_tests_form_one_cycle_when_reassessed_together():
    store, facts = fixture()
    review, row = transition(store, facts)
    selected_action = facts['reasoning_activity']['acquisitions'][1]
    row['action_ids'] = [selected_action['id']]
    row['evidence_refs'].append(review.present(selected_action['request_ref'], max_chars=10000)['evidence_id'])
    first, issues = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    assert not issues
    review2, row2 = transition(store, facts, 2)
    later, issues = validate_transitions({'reasoning_transitions': [row2]}, review2, [row2['segment_id']], facts)
    p = build_reasoning_process(facts, {'reasoning_transitions': first + later})
    assert len(p['cycles']) == 1
    assert p['cycles'][0]['status'] == 'closed'
    assert p['cycles'][0]['end_line'] == 10


def test_acceptance_pass_cannot_be_promoted_to_root_cause_verification():
    events = [*action(1), *action(2), {'type': 'acceptance.gate.passed', 'assurance': 'model_judgment'}, *action(3)]
    store = source(events)
    facts = build_behavior_facts(store)
    review, row = transition(store, facts)
    first, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    review2, row2 = transition(store, facts, 2)
    acceptance = next(o for o in facts['sequence']['observations'] if o['kind'] == 'verification_result')
    row2.update(claim_id=first[0]['claim_id'], status='verified_within_scope', observation_ids=[acceptance['id']],
                rationale='Everything passed')
    row2['evidence_refs'] = [review2.present(acceptance['evidence_refs'][0], max_chars=10000)['evidence_id'],
                            review2.present(first[0]['assertion_ref'], max_chars=10000)['evidence_id']]
    context, _ = reasoning_context(facts, facts['segments'][2], first)
    assert not validate_transitions({'reasoning_transitions': [row2]}, review2, [row2['segment_id']], facts, context)[0]


def test_public_claim_cannot_be_accepted_from_a_truncated_undelivered_range():
    store, facts = fixture()
    review, row = transition(store, facts)
    other = EvidenceReview(store)
    other.present(facts['model_calls'][1]['response_ref'], max_chars=1)
    row['assertion_ref'] = review.citation(row['assertion_ref'])
    assert not validate_transitions({'reasoning_transitions': [row]}, other, [row['segment_id']], facts)[0]


def test_empty_semantic_from_old_facts_does_not_reinterpret_legacy_relationships():
    _, facts = fixture()
    del facts['reasoning_activity']
    result = partial_semantic(facts, {})
    assert 'reasoning_process' not in result


def test_recorded_frozen_operation_metadata_and_failed_read_attempts_are_counted():
    events = [*action(1, 'custom'), *action(2)]
    events[2]['metadata'] = {'operations': [{'kind': 'read'}, {'kind': 'execute'}]}
    events[7] = {**events[7], 'type': 'tool.failed', 'error': {'code': 'MISSING', 'message': 'Missing file'}}
    facts = build_behavior_facts(source(events))
    assert facts['reasoning_activity']['acquisitions'][0]['classification'] == 'mixed'
    p = build_reasoning_process(facts)
    costs = p['scopes'][0]['costs_by_role']['solver']
    assert costs['reads'] == 1
    assert costs['read_failed'] == 1
    assert costs['read_successes'] == 0
    assert costs['read_including_mixed'] == 2


def test_scoped_verification_follows_support_and_keeps_recorded_assurance():
    events = [*action(1), *action(2), {'type': 'verification.completed',
               'result': {'status': 'passed', 'assurance': 'deterministic', 'reason': 'Discriminating regression check passed'}}, *action(3)]
    store = source(events)
    facts = build_behavior_facts(store)
    review, row = transition(store, facts)
    first, _ = validate_transitions({'reasoning_transitions': [row]}, review, [row['segment_id']], facts)
    review2, row2 = transition(store, facts, 2)
    check = next(o for o in facts['sequence']['observations'] if o['kind'] == 'verification_result')
    row2.update(claim_id=first[0]['claim_id'], status='verified_within_scope', observation_ids=[check['id']],
                rationale='The discriminating regression test targets the configuration cause')
    row2['evidence_refs'] = [review2.present(check['evidence_refs'][0], max_chars=10000)['evidence_id'],
                            review2.present(first[0]['assertion_ref'], max_chars=10000)['evidence_id']]
    context, _ = reasoning_context(facts, facts['segments'][2], first)
    verified, issues = validate_transitions({'reasoning_transitions': [row2]}, review2, [row2['segment_id']], facts, context)
    assert not issues
    p = build_reasoning_process(facts, {'reasoning_transitions': first + verified})
    endpoint = p['questions'][0]['endpoint']
    assert endpoint['verified_id'] == verified[0]['id']
    assert endpoint['cause_to_verification']['model_calls'] == 1
    assert next(n for n in p['nodes'] if n['kind'] == 'check')['assurance'] == 'deterministic'


def test_focus_pipeline_saves_process_and_resumes_without_extra_model_calls():
    import asyncio
    import json

    from loom.core import ok
    from loom.evaluation.behavior_contracts import DIMENSIONS
    from loom.evaluation.behavior_judge import judge_behavior
    from loom.llm import LlmResponse, TokenUsage

    events = [*action(1), *action(2)]
    events[5]['response']['content'] = 'The observed configuration explains the failure; this supports the configuration cause.'
    store = source(events)
    facts = build_behavior_facts(store)

    class Judge:
        model = 'offline-reasoning-fixture'
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None):
            self.calls += 1
            payload = json.loads(messages[1].content)
            if payload['stage'].startswith('scan:'):
                result = dict(candidate_segment_ids=[s['id'] for s in payload['segment_index']], control_segment_ids=[])
            elif payload['stage'].startswith('focus:'):
                sid = payload['segments'][0]['id']
                result = dict(assessments=[dict(segment_id=sid, dimension=d, status='unknown', rationale='Fixture', evidence_refs=[])
                                          for d in DIMENSIONS])
                if sid == facts['segments'][1]['id']:
                    packets = payload['evidence_packets']
                    assertion = next(p for p in packets if p['returned_ref']['line_number'] == 6)
                    support = next(p for p in packets if p['returned_ref']['line_number'] == 4)
                    result['reasoning_transitions'] = [dict(segment_id=sid, question_kind='diagnostic', claim_type='cause',
                        label='Configuration cause', status='supported', assertion_ref=assertion['evidence_id'],
                        evidence_refs=[support['evidence_id']], observation_ids=[facts['tool_uses'][0]['id']],
                        action_ids=[], evidence_use='adopted')]
            else:
                assert payload['reasoning_metrics']
                result = dict(summary='Configuration evidence supports the public cause claim.', finding_ids=[], limitations=[])
            return ok(LlmResponse(json.dumps(result), usage=TokenUsage(total_tokens=100)))

    judge, checkpoint = Judge(), {}
    result = asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint, max_segments=2)).unwrap()
    assert result['reasoning_process']['questions'][0]['endpoint']['cost']['reads'] == 1
    assert judge.calls == 4  # one scan + two focus + existing synthesis
    again = asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint, max_segments=2)).unwrap()
    assert judge.calls == 4
    assert again['reasoning_process'] == result['reasoning_process']


def test_process_projection_keeps_public_purpose_and_results_separate_from_evidence_judgments():
    import json

    events = [{'type': 'acceptance.gate.blocked', 'reason': 'Before work'}, *action(1)]
    events[2]['response']['content'] = json.dumps({'reasoning': 'Compare orbital parameters before making a conclusion.',
        'action': {'kind': 'tool', 'description': 'Read planet periods and distances'}})
    facts = build_behavior_facts(source(events))
    p = build_reasoning_process(facts)
    graph = p['graph']
    assert graph['questions']
    assert [n['phase'] for n in graph['nodes']] == ['question', 'judgment', 'collect', 'observation', 'outcome']
    assert graph['nodes'][-1]['terminal'] is False
    assert graph['nodes'][1]['status'] == 'recorded_statement'
    assert graph['nodes'][2]['label'] == 'Read planet periods and distances'
    assert graph['nodes'][3]['label'] != graph['nodes'][2]['label']
    assert {'emits', 'returns'} <= {r['kind'] for r in graph['relations']}
    assert all(r['layer'] == 'flow' for r in graph['relations'])
    assert not any(n['kind'] == 'check' for n in graph['nodes'])
    assert not p['occurrences']


def test_delivered_navigation_binds_each_assertion_to_prior_results_and_its_own_action():
    from loom.evaluation.reasoning_process import delivered_context

    store, facts = fixture()
    review, row = transition(store, facts)
    segment = facts['segments'][1]
    context, _ = reasoning_context(facts, segment)
    context = delivered_context(context, review)
    assertion = context['assertions'][0]
    assert assertion['assertion_refs'] == [row['assertion_ref']]
    assert assertion['prior_observation_ids'] == row['observation_ids']
    obs = next(o for o in context['observations'] if o['id'] == row['observation_ids'][0])
    assert obs['result_citations'] == row['evidence_refs']
    action = next(a for a in context['actions'] if a['id'] in assertion['subsequent_action_ids'])
    assert action['input_citations'] == []  # navigation alone is not delivered evidence


def test_reasoning_repair_is_bounded_and_preserves_deep_judgments():
    import asyncio
    import json

    from loom.core import ok
    from loom.evaluation.behavior_judge import judge_behavior
    from loom.llm import LlmResponse, TokenUsage

    store, facts = fixture()
    target = facts['segments'][1]

    class Judge:
        model = 'reasoning-repair-fixture'
        repairs = 0

        async def chat(self, messages, tools=None, cancellation=None):
            p = json.loads(messages[1].content)
            if p['stage'].startswith('scan:'):
                value = dict(candidate_segment_ids=[target['id']], control_segment_ids=[target['id']])
            elif p['stage'].startswith('focus:'):
                if 'errors' in p:
                    self.repairs += 1
                    value = {'reasoning_transitions': p['proposed']}
                    value['reasoning_transitions'][0]['evidence_refs'] = next(
                        o['result_citations'] for o in p['reasoning_context']['observations']
                        if o['id'] == value['reasoning_transitions'][0]['observation_ids'][0])
                else:
                    assertion = p['reasoning_context']['assertions'][0]
                    value = {'progress': [{'segment_id': target['id'], 'kind': 'decision', 'label': 'Choose a test',
                                          'evidence_refs': assertion['assertion_refs']}],
                             'reasoning_transitions': [dict(segment_id=target['id'], question_kind='diagnostic', claim_type='cause',
                                label='Configuration cause', status='supported', assertion_ref=assertion['assertion_refs'][0],
                                observation_ids=assertion['prior_observation_ids'], action_ids=[], evidence_refs=[], evidence_use='adopted')]}
            else:
                value = dict(summary='Done', finding_ids=[], limitations=[])
            return ok(LlmResponse(json.dumps(value), usage=TokenUsage(total_tokens=100)))

    judge, checkpoint = Judge(), {}
    result = asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint, max_segments=1)).unwrap()
    assert judge.repairs == 1
    assert len(result['reasoning_transitions']) == 1
    assert result['progress'][0]['label'] == 'Choose a test'
    assert not result['reasoning_diagnostics']
    assert checkpoint['usage']['calls'] == 4
    asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint, max_segments=1)).unwrap()
    assert checkpoint['usage']['calls'] == 4


def test_multi_round_focus_delivers_late_assertions_and_numeric_result_tails():
    import asyncio
    import json

    from loom.core import ok
    from loom.evaluation.behavior_judge import judge_behavior
    from loom.llm import LlmResponse, TokenUsage

    events = []
    for n in range(1, 5):
        batch = action(n, 'shell_execute', {'command': f'cat data{n}.ts; node check.js'},
                       {'value': {'stdout': 'source code\n' * 1000 + f'NUMERIC RESULT {n}: 29.78 km/s', 'exit_code': 0}})
        for e in batch:
            e.update(step_number=0, trace_id='same-step')
        events += batch
    store = source(events)
    facts = build_behavior_facts(store)
    assert len(facts['segments']) == 1

    class Judge:
        model = 'multi-round-source-regression'

        async def chat(self, messages, tools=None, cancellation=None):
            p = json.loads(messages[1].content)
            if p['stage'].startswith('scan:'):
                value = dict(candidate_segment_ids=[facts['segments'][0]['id']], control_segment_ids=[])
            elif p['stage'].startswith('focus:'):
                assert len(messages[0].content) + len(messages[1].content) < 48000
                packets = p['evidence_packets']
                assert {r['returned_ref']['line_number'] for r in packets if r['returned_ref']['field_path'] == 'response.content'} == {2, 6, 10, 14}
                assert any('NUMERIC RESULT 3' in r['content'] for r in packets)
                assert all(a['assertion_refs'] for a in p['reasoning_context']['assertions'])
                value = {}
            else:
                value = dict(summary='Sources delivered', finding_ids=[], limitations=[])
            return ok(LlmResponse(json.dumps(value), usage=TokenUsage(total_tokens=100)))

    asyncio.run(judge_behavior(store, facts, Judge(), max_prompt_chars=48000)).unwrap()
