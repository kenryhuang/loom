import json
from copy import deepcopy

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.action_classification import inspection_command
from loom.evaluation.goal_loops.execution_view import execution_view
from loom.evaluation.goal_loops.review import project
from tests.evaluation.test_behavior_sequence import action, source


def json_action(command, description="Run numeric audit script", *, target="shell_execute", actual=None):
    events = action(1, tool="shell_execute", inp={"command": command if actual is None else actual})
    for event in events:
        if event['type'].startswith('llm.'):
            event['llm_call_id'] = 'trace1-llm-3'
        else:
            event.pop('llm_call_id')
            event['tool_call_id'] = 'trace1-json-0-3'
    events[1]['response']['content'] = '```json\n' + json.dumps({'action': {
        'kind': 'tool', 'target': target, 'description': description, 'input': {'command': command}}}) + '\n```'
    store = source(events)
    bundle = project(store, build_behavior_facts(store), 'run', {})
    return store, bundle


@pytest.mark.parametrize('command', [
    "ls -la && echo '---' && find . -type f | grep -v node_modules | head -100",
    'for f in a.ts b.ts; do echo "===== $f ====="; cat -n "$f"; done',
    "echo 'Files'; cat a.ts; echo; grep -rn 'scale\\|speed' src docs 2>/dev/null | head -40",
])
def test_read_only_command_chains_and_file_loops(command):
    assert inspection_command(command)
    _, bundle = json_action(command)
    flow = bundle['execution_view']['action_flow']
    assert [a['kind'] for a in flow['actions']] == ['research', 'research']
    assert all(not a['classification_inferred'] for a in flow['actions'])


@pytest.mark.parametrize('command', [
    'find . -delete', 'find . -exec rm {} +', 'find . -fprintf out.txt %p',
    'cat a > copied.txt', 'cat a; rm a', 'cat $(touch bad)', 'cat `touch bad`',
    'cat a | xargs rm', 'rg --pre=command foo', 'for f in $(command); do cat "$f"; done',
    'node /tmp/verify.mjs', 'echo verification',
])
def test_unknown_or_mutating_commands_do_not_become_research(command):
    assert not inspection_command(command)


def test_managed_json_actions_link_and_declared_intent_is_cited_not_assumed_success():
    _, bundle = json_action("node /tmp/arbitrary-name.mjs")
    view = bundle['execution_view']
    assert len(view['rounds']) == 1
    assert view['edges'][0]['basis'] == 'legacy_id_convention'
    model, tool = view['action_flow']['actions']
    assert model['title'] == 'Working · Preparing shell_execute'
    for row in (model, tool):
        assert row['kind'] == 'verification'
        assert row['classification_inferred']
        assert row['classification_evidence']
        assert row['basis'] == ['inferred from declared action purpose']
    assert bundle['action_utility']['coverage']['reviewed'] == 0  # No evaluator judgment is manufactured.


@pytest.mark.parametrize('kwargs', [{'target': 'read_file'}, {'actual': 'node different.mjs'}, {'description': 'Maybe investigate'}])
def test_declared_purpose_requires_matching_target_input_and_clear_intent(kwargs):
    _, bundle = json_action('node /tmp/verify.mjs', **kwargs)
    assert all(a['kind'] == 'other' for a in bundle['execution_view']['action_flow']['actions'])


def test_full_command_not_truncated_target_drives_classification():
    _, bundle = json_action('cat ' + 'a ' * 150 + '; touch b', description='Read files')
    assert len(bundle['execution_view']['calls'][1]['target']) == 240
    assert all(a['kind'] == 'other' for a in bundle['execution_view']['action_flow']['actions'])


def test_saved_assessment_requires_own_citations_and_does_not_change_ledger():
    store, bundle = json_action('node arbitrary.mjs', description='Do work')
    model, tool = bundle['execution_view']['calls']
    original = deepcopy(bundle['cost_ledger'])
    assessment = dict(call_id=tool['id'], status='reviewed', role='verification', confidence='high',
                      reason='Numerically cross-checks acceptance criteria', evidence_refs=tool['observation_refs'])
    bundle['action_utility']['rows'] = [assessment]
    view = execution_view(store, bundle)
    assert all(a['kind'] == 'verification' for a in view['action_flow']['actions'])
    assert view['action_flow']['actions'][0]['basis'] == ['inferred from saved action assessment']
    assert bundle['cost_ledger'] == original
    assessment['evidence_refs'] = model['statement_refs']
    view = execution_view(store, bundle)
    assert all(a['kind'] == 'other' for a in view['action_flow']['actions'])


def test_phase_window_alone_never_assigns_a_purpose_and_conflicting_citations_remain_unknown():
    store, bundle = json_action('node arbitrary.mjs', description='Do work')
    tool = bundle['execution_view']['calls'][1]
    phase = dict(id='p', kind='verification', confidence='high', start_line=1, end_line=4,
                 evidence_refs=tool['observation_refs'], call_ids=[], question='Check result')
    bundle['phases'] = [phase]
    assert execution_view(store, bundle)['calls'][1]['classification']['kind'] == 'other'
    phase['call_ids'] = [tool['id']]
    view = execution_view(store, bundle)
    assert view['calls'][1]['classification']['kind'] == 'verification'
    bundle['phases'].append({**phase, 'id': 'p2', 'kind': 'investigation'})
    view = execution_view(store, bundle)
    assert view['calls'][1]['classification']['kind'] == 'other'
    assert view['calls'][1]['classification']['basis'] == 'conflicting saved phase assessments'
