"""Flow must be complete without claiming unsupported causal/evidence relationships."""

import json

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.reasoning_metrics import build_reasoning_process
from tests.evaluation.test_behavior_sequence import action, source


def process(events):
    return build_reasoning_process(build_behavior_facts(source(events)))


def assert_connected(graph):
    nodes = {n['id']: n for n in graph['nodes']}
    flow = [r for r in graph['relations'] if r['layer'] == 'flow']
    for scope in graph['boundaries']:
        root, end = nodes[scope['start_id']], nodes[scope['end_id']]
        ids = {n['id'] for n in nodes.values() if (n['episode_id'], n['goal_revision_id']) == (root['episode_id'], root['goal_revision_id'])}
        def reachable(seed, reverse=False):
            seen, todo = {seed}, [seed]
            while todo:
                n = todo.pop()
                for r in flow:
                    a, b = (r['target'], r['source']) if reverse else (r['source'], r['target'])
                    if a == n and b not in seen:
                        seen.add(b)
                        todo.append(b)
            return seen
        assert reachable(root['id']) == ids
        assert reachable(end['id'], True) == ids
        assert not any(r['source'] == end['id'] for r in graph['relations'])
        assert not any(r['target'] == root['id'] for r in graph['relations'])
    assert all(nodes[r['source']]['episode_id'] == nodes[r['target']]['episode_id'] for r in graph['relations'])


def test_rejected_completion_retry_report_and_runtime_terminal_are_connected_without_deep():
    events = [*action(1), *action(2, 'complete_node', {'evidence': 'Source checked'},
              {'accepted': False, 'reason': 'Use finish for the final report'}),
              *action(3, 'finish', {'report': '# Research report'}, {'completed': True, 'report': '# Research report'}),
              {'type': 'run.completed'}]
    graph = process(events)['graph']
    assert_connected(graph)
    rejection = next(n for n in graph['nodes'] if n['phase'] == 'rejection')
    later = next(n for n in graph['nodes'] if n['kind'] == 'model_response' and n['line'] > rejection['line'])
    assert any(r['source'] == rejection['id'] and r['target'] == later['id'] and r['kind'] == 'continues_after_failure'
               for r in graph['relations'])
    assert graph['nodes'][-1]['label'] == 'Task completed · report formed'
    assert graph['nodes'][-1]['terminal'] is True
    assert all(r['layer'] == 'flow' for r in graph['relations'])


@pytest.mark.parametrize('status,terminal', [('failed', True), ('stopped', True), ('paused', False), ('suspended', False), ('running', False)])
def test_boundaries_follow_recorded_run_state_not_agent_completion_claims(status, terminal):
    events = [*action(1, 'finish', {'report': 'I am done'}, {'accepted': False, 'reason': 'Verification failed'}),
              {'type': 'run.state.changed', 'state': status, 'reason': 'Recorded reason'}]
    graph = process(events)['graph']
    assert_connected(graph)
    end = graph['nodes'][-1]
    assert (end['status'], end['terminal']) == (status, terminal)
    assert end['report_formed'] is False
    assert end['evidence_refs'][0]['line_number'] == len(events)
    assert end['content'] == 'Recorded reason'


def test_failure_before_any_model_call_still_has_a_terminal_and_error_evidence():
    graph = process([{'type': 'run.started'}, {'type': 'run.failed', 'message': 'Provider unavailable'}])['graph']
    assert_connected(graph)
    assert graph['nodes'][-1]['status'] == 'failed'
    assert graph['nodes'][-1]['content'] == 'Provider unavailable'


def test_truncated_trace_is_an_open_boundary_not_a_fabricated_success_or_failure():
    graph = process(action(1)[:3])['graph']
    assert_connected(graph)
    assert graph['nodes'][-1]['terminal'] is False
    assert graph['nodes'][-1]['status'] == 'unknown'
    assert not any(n.get('completed') for n in graph['nodes'])


def test_failed_tools_retain_error_result_in_the_loop():
    events = action(1)
    events[-1] = {**events[-1], 'type': 'tool.failed', 'error': {'message': 'File missing'}}
    events[-1].pop('output', None)
    graph = process(events + action(2))['graph']
    assert_connected(graph)
    failure = next(n for n in graph['nodes'] if n['phase'] == 'failure')
    assert 'File missing' in failure['content']


def test_verification_round_keeps_per_check_evidence_and_joins_into_report_outcome():
    first, final = action(1), action(2, 'finish', {'report': '# Report'}, {'completed': True, 'report': '# Report'})
    events = [*first, *final[:3], {'type': 'verification.completed', 'result': {'status': 'passed', 'criterion_id': 'a', 'reason': 'Data checked'}},
              {'type': 'verification.completed', 'result': {'status': 'passed', 'criterion_id': 'b', 'reason': 'Numbers checked'}},
              {'type': 'acceptance.gate.passed'}, final[3], {'type': 'run.completed'}]
    graph = process(events)['graph']
    assert_connected(graph)
    checks = [n for n in graph['nodes'] if n['phase'] == 'verification']
    assert len(checks) == 1
    assert checks[0]['label'] == 'Acceptance passed · 2 checks'
    assert len(checks[0]['verification_results']) == 3
    assert len(checks[0]['evidence_refs']) >= 3
    report = next(n for n in graph['nodes'] if n['phase'] == 'report')
    assert any(r['source'] == checks[0]['id'] and r['target'] == report['id'] and r['layer'] == 'flow'
               for r in graph['relations'])


@pytest.mark.parametrize('accepted', [True, False])
def test_finish_result_requires_success_to_establish_completion_without_run_terminal(accepted):
    graph = process(action(1, 'finish', {'report': '# Report'},
                           {'accepted': accepted, 'completed': True, 'report': '# Report'}))['graph']
    assert_connected(graph)
    end = graph['nodes'][-1]
    assert end['terminal'] is accepted
    assert end['report_formed'] is accepted
    assert end['status'] == ('completed' if accepted else 'unknown')


def test_parallel_tool_branches_are_joined_without_serializing_each_other():
    first = action(1)
    second = [{**e, 'tool_call_id': 'other-tool'} for e in first[2:]]
    events = [*first[:3], second[0], second[1], first[3], *action(2), {'type': 'run.completed'}]
    graph = process(events)['graph']
    assert_connected(graph)
    results = [n for n in graph['nodes'] if n['phase'] == 'observation' and n['line'] < 7]
    next_round = next(n for n in graph['nodes'] if n['kind'] == 'model_response' and n['line'] > 6)
    assert len(results) == 2
    assert all(any(r['source'] == n['id'] and r['target'] == next_round['id'] for r in graph['relations']) for n in results)
    assert not any(r['source'] == results[0]['id'] and r['target'] == results[1]['id'] for r in graph['relations'])


def test_goal_revision_has_its_own_boundary_and_never_leaks_flow_or_completion():
    events = [*action(1), {'type': 'task.goal.revised', 'objective': 'New goal', 'input_cursor': 1},
              *action(2), {'type': 'run.completed'}]
    graph = process(events)['graph']
    assert_connected(graph)
    assert [b['status'] for b in graph['boundaries']] == ['superseded', 'completed']
    nodes = {n['id']: n for n in graph['nodes']}
    assert all(nodes[r['source']]['goal_revision_id'] == nodes[r['target']]['goal_revision_id'] for r in graph['relations'])


def test_real_shape_final_statements_have_incoming_and_outgoing_flow():
    events = []
    for n, tool in enumerate(['shell_execute', 'complete_node', 'finish'], 1):
        output = {'accepted': False, 'reason': 'Use finish'} if tool == 'complete_node' else {'completed': True, 'report': '# Report'}
        batch = action(n, tool, {'command': 'inspect', 'report': '# Report'}, output)
        batch[1]['response']['content'] = json.dumps({'reasoning': 'Summarize findings' if n < 3 else 'Host rejected completion; submit report',
                                                     'action': {'description': tool}})
        events += batch
    graph = process(events + [{'type': 'run.completed'}])['graph']
    assert_connected(graph)
    for n in graph['nodes']:
        if n['kind'] == 'model_response':
            assert any(r['target'] == n['id'] for r in graph['relations'])
            assert any(r['source'] == n['id'] for r in graph['relations'])
