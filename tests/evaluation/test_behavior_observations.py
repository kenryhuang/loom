from dataclasses import asdict

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import _validate_focus
from loom.evaluation.behavior_sequence import focus_context
from loom.evaluation.evidence_review import EvidenceReview
from tests.evaluation.test_behavior_sequence import action, source


def plan(kind='plan.submitted', active='b', a_status='pending'):
    return {'type': kind, 'workflow': {'active': active}, 'plan': {'items': [
        {'id': 'a', 'content': 'Collect evidence', 'status': a_status},
        {'id': 'b', 'content': 'Verify', 'status': 'in_progress', 'dependencies': ['a']},
        {'id': 'unused', 'content': 'Alternative path', 'status': 'pending'}]}}


def test_plan_checks_use_effective_snapshot_and_declared_dependencies_only():
    facts = build_behavior_facts(source([plan('plan.entered'), *action(1), plan(), *action(2),
                                        plan(a_status='completed'), *action(3)]))
    rows = facts['sequence']['plan_bindings']
    assert rows[0]['status'] == 'no_effective_plan'
    assert rows[1]['unmet_dependencies'] == ['a']
    assert rows[2]['unmet_dependencies'] == []
    assert facts['sequence']['plan_summary']['dependency_violations'] == 1
    assert facts['sequence']['plan_summary']['nodes_without_recorded_calls'] == 2
    assert all(p['evidenced_done'] == 'unknown' for p in facts['sequence']['plan_execution'])


def test_goal_change_and_empty_effective_plan_do_not_reuse_old_nodes():
    facts = build_behavior_facts(source([plan(), *action(1),
        {'type': 'task.goal.revised', 'objective': 'New requirement', 'goal_revision': 2, 'input_cursor': 2}, *action(2),
        {'type': 'plan.updated', 'plan': {'items': []}}, *action(3)]))
    rows = facts['sequence']['plan_bindings']
    assert rows[0]['node_id'] == 'b'
    assert rows[1]['status'] == 'no_effective_plan'
    assert rows[2]['status'] == 'no_active_node'


def test_composite_commands_compare_actual_results_and_deduplicate_associated_cost():
    events = []
    for n in range(3):
        events += action(n, 'shell_execute', {'command': f'cat a.txt && echo {n}'},
                         {'stdout': 'identical returned text', 'stderr': '', 'exit_code': 0,
                          'duration_ms': 10 + n, 'argv': [f'different command {n}']})
    facts = build_behavior_facts(source(events))
    sequence = facts['sequence']
    assert sequence['observation_coverage']['resource_identity_calls'] == 0
    assert sequence['observation_coverage']['comparable_result_calls'] == 3
    assert len(sequence['repetitions']) == 2
    assert all(r['kind'] == 'same_tool_result' and not r['same_input'] for r in sequence['repetitions'])
    assert sequence['repetition_cost']['known_tool_ms'] == 23
    assert sequence['repetition_cost']['associated_known_tokens'] == 30
    assert sequence['repetition_cost']['tool_calls'] == 2
    assert not sequence['candidate_windows']  # Different probes with equal results are facts, not automatically suspicious.


def test_repeated_results_do_not_cross_goals_and_failures_are_not_comparable_results():
    a = action(1, 'shell_execute', {'command': 'some command'}, {'stdout': 'same', 'exit_code': 0})
    b = action(2, 'shell_execute', {'command': 'some command'}, {'stdout': 'same', 'exit_code': 0})
    facts = build_behavior_facts(source([*a, {'type': 'task.goal.revised', 'objective': 'Different task', 'input_cursor': 2}, *b]))
    assert not facts['sequence']['repetitions']


def test_explicit_decisions_verification_and_costs_exist_without_a_judge():
    events = [{'type': 'task.goal.revised', 'goal_revision': 1, 'input_cursor': 1, 'objective': 'Investigate the problem'},
              *action(1), {'type': 'decision.recorded', 'decision': 'Test the cache hypothesis'},
              {'type': 'verification.completed', 'goal_revision': 0, 'result': {'status': 'passed'}},
              {'type': 'verification.completed', 'goal_revision': 1, 'result': {'status': 'failed'}},
              {'type': 'verification.completed', 'goal_revision': 1, 'result': {'status': 'passed', 'assurance': 'model_judgment'}},
              {'type': 'acceptance.gate.passed', 'goal_revision': 1}]
    for n, event in enumerate(events):
        event['at'] = f'2026-10-11T00:00:{n:02d}+00:00'
    facts = build_behavior_facts(source(events))
    seq = facts['sequence']
    milestones = {r['kind']: r for r in seq['milestones']}
    assert milestones['first_verification_pass']['line'] == 9
    assert milestones['first_acceptance_pass']['line'] == 10
    assert milestones['first_decision']['cost']['known_tokens'] == 15
    assert milestones['first_decision']['cost']['elapsed_ms'] == 5000
    assert sum(r['since_previous_observation']['known_tokens'] for r in seq['observations']) == 15
    context = focus_context(facts, facts['segments'][0])
    assert any(r['kind'] == 'verification_result' for r in context['activity_signals'])
    assert any(r['kind'] == 'verification_result' and r['goal_binding'] == 'mismatch' for r in context['behavior_facts'])
    assert any(r['kind'] == 'decision_recorded' for r in context['behavior_facts'])


def test_fact_judgments_require_both_repeat_endpoints_and_cannot_invent_fact_ids():
    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    primary = facts['segments'][-1]
    repeat = next(r for r in focus_context(facts, primary)['behavior_facts'] if r['kind'] == 'same_resource_content')
    review = EvidenceReview(store)
    a = review.present(asdict(store.ref(store.events[3], 'output.content')), max_chars=100)
    b = review.present(asdict(store.ref(store.events[7], 'output.content')), max_chars=100)
    row = dict(fact_id=repeat['id'], status='ineffective', rationale='Same read without intervening recorded changes')
    result = _validate_focus({'fact_assessments': [{**row, 'evidence_refs': [b['evidence_id']]}]}, review, [primary['id']], facts)
    assert next(r for r in result['fact_assessments'] if r['fact_id'] == repeat['id'])['status'] == 'unknown'
    result = _validate_focus({'fact_assessments': [{**row, 'evidence_refs': [a['evidence_id'], b['evidence_id']]},
        {**row, 'fact_id': 'invented', 'evidence_refs': [b['evidence_id']]}]}, review, [primary['id']], facts)
    assert next(r for r in result['fact_assessments'] if r['fact_id'] == repeat['id'])['status'] == 'ineffective'
    assert all(r['fact_id'] != 'invented' for r in result['fact_assessments'])
    assert result['validation_issues']


def test_terminal_verification_can_anchor_progress_without_owning_an_extra_segment():
    store = source([*action(1), {'type': 'acceptance.gate.passed', 'acceptance': {'state': 'passed'}}])
    facts = build_behavior_facts(store)
    primary = facts['segments'][0]
    fact = next(r for r in focus_context(facts, primary)['behavior_facts'] if r['kind'] == 'verification_result')
    review = EvidenceReview(store)
    packet = review.present(asdict(store.ref(store.events[-1])), max_chars=2000)
    result = _validate_focus({'progress': [{'segment_id': primary['id'], 'kind': 'verification', 'label': 'Recorded gate passed',
        'evidence_refs': [packet['evidence_id']], 'fact_ids': [fact['id']]}]}, review, [primary['id']], facts)
    assert result['progress'][0]['fact_ids'] == [fact['id']]
    assert result['progress'][0]['line'] == 5


def test_indexed_fact_costs_match_interval_measurement_without_cumulative_id_copies():
    from loom.evaluation.behavior_progress import interval_cost
    from loom.evaluation.observation_cost import observation_cost_counter

    facts = build_behavior_facts(source([*action(1), *action(2)]))
    facts['token_ledger'][0]['total_tokens'] = None
    cost = observation_cost_counter(facts)
    episode = facts['episodes'][0]['id']
    for start, end, exclusive in [(1, 4, False), (4, 8, True), (1, 8, False), (4, 4, True)]:
        indexed = cost(start, end, episode, exclusive_start=exclusive)
        reference = interval_cost(facts, start, end, episode, exclusive_start=exclusive)
        assert indexed == {k: v for k, v in reference.items() if k not in {'round_ids', 'tool_ids'}}


def test_fact_interpretations_are_saved_synthesized_and_reused_on_resume():
    import asyncio
    import json
    from dataclasses import replace

    from loom.core import ok
    from loom.evaluation.behavior_judge import judge_behavior
    from tests.evaluation.test_behavior import BehaviorJudge

    class Judge(BehaviorJudge):
        async def chat(self, messages, tools=None, cancellation=None):
            response = await super().chat(messages, tools, cancellation)
            payload = json.loads(messages[1].content)
            if payload['stage'].startswith('focus:'):
                value = json.loads(response.value.content)
                value['fact_assessments'] = []
                for fact in payload['behavior_facts']:
                    required = {fact.get('action_line', fact['line'])}
                    if fact.get('first_line'):
                        required.add(fact['first_line'])
                    citations = [p['evidence_id'] for p in payload['evidence_packets'] if p['returned_ref']['line_number'] in required]
                    value['fact_assessments'].append(dict(fact_id=fact['id'], status='effective', rationale='Recorded evidence is useful',
                                                         evidence_refs=citations))
                return ok(replace(response.value, content=json.dumps(value)))
            if payload['stage'] == 'synthesis':
                assert payload['fact_assessments']
                assert any(r['status'] == 'effective' for r in payload['fact_assessments'])
            return response

    store = source(action(1))
    facts, cp, judge = build_behavior_facts(store), {}, Judge()
    result = asyncio.run(judge_behavior(store, facts, judge, checkpoint=cp)).unwrap()
    assert result['coverage']['fact_assessments']['supported'] > 0
    assert all(r['fact_id'] in {f['id'] for f in facts['sequence']['observations']} for r in result['fact_assessments'])
    count = judge.calls
    assert asyncio.run(judge_behavior(store, facts, judge, checkpoint=cp)).unwrap()['fact_assessments'] == result['fact_assessments']
    assert judge.calls == count


def test_no_new_result_window_is_a_citable_fact_not_a_waste_verdict():
    events = []
    for n in range(11):
        events += action(n, 'search', {'query': str(n)}, {'results': []})
    facts = build_behavior_facts(source(events))
    rows = [r for r in facts['sequence']['observations'] if r['kind'] == 'no_new_result_window']
    assert len(rows) == 1 and rows[0]['tool_calls'] == 10
    assert rows[0]['status'] == 'candidate'
    assert rows[0]['semantic_progress'] == 'not_assessed'
    assert any(r['id'] == rows[0]['id'] for r in focus_context(facts, facts['segments'][-1])['behavior_facts'])


def test_plan_fact_accepts_actual_tool_result_but_not_plan_text_alone():
    store = source([plan(a_status='completed'), *action(1)])
    facts = build_behavior_facts(store)
    primary = facts['segments'][0]
    fact = next(r for r in focus_context(facts, primary)['behavior_facts'] if r['kind'] == 'plan_binding')
    review = EvidenceReview(store)
    plan_ref = review.present(asdict(store.ref(store.events[0])), max_chars=10000)
    tool_ref = review.present(asdict(store.ref(store.events[-1], 'output.content')), max_chars=1000)
    row = dict(fact_id=fact['id'], status='effective', rationale='Actual result serves the plan')
    a = _validate_focus({'fact_assessments': [{**row, 'evidence_refs': [plan_ref['evidence_id']]}]}, review, [primary['id']], facts)
    assert next(r for r in a['fact_assessments'] if r['fact_id'] == fact['id'])['status'] == 'unknown'
    b = _validate_focus({'fact_assessments': [{**row, 'evidence_refs': [tool_ref['evidence_id']]}]}, review, [primary['id']], facts)
    assert next(r for r in b['fact_assessments'] if r['fact_id'] == fact['id'])['status'] == 'effective'
