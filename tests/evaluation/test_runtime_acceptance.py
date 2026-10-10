from loom.evaluation.behavior_facts import build_behavior_facts
from tests.evaluation.test_behavior import calls, source


def test_runtime_acceptance_and_verifier_usage_are_recorded_without_claiming_ground_truth(tmp_path):
    events = calls(2)
    events[2]['usage_role'] = 'verification'
    events[2]['messages'] = [{'role': 'user', 'content': 'Judge this unrelated acceptance payload'}]
    events += [{'type': 'acceptance.gate.passed', 'producer': 'loom.acceptance.v1',
                'acceptance': {'state': 'passed', 'results': [{'criterion_id': 'goal', 'status': 'passed', 'assurance': 'model_judgment'}]}},
               {'type': 'run.completed'}]
    facts = build_behavior_facts(source(tmp_path, events))
    assert facts['base']['metrics']['model_calls'] == 2
    assert facts['base']['metrics']['verification_model_calls'] == 1
    assert facts['base']['runtime_acceptance']['state']['state'] == 'passed'
    assert facts['base']['task_completion'] == 'unverified'
    assert sum(len(s['round_ids']) for s in facts['segments']) == 1
    assert not any('unrelated acceptance' in g['objective'] for g in facts['goal_revisions'])
