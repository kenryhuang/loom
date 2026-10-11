import asyncio
import json
from dataclasses import asdict

import pytest

from loom.evaluation.behavior_contracts import DIMENSIONS
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import _validate_focus, judge_behavior
from loom.evaluation.evidence_review import EvidenceReview
from loom.evaluation.focus_evidence import focus_evidence_refs
from tests.evaluation.test_behavior import BehaviorJudge, calls, source


def test_short_citations_bind_only_delivered_ranges_and_explain_unknown(tmp_path):
    store = source(tmp_path, calls(1))
    review = EvidenceReview(store)
    full = asdict(store.ref(store.events[-1], 'response.content'))
    packet = review.present(full, max_chars=5)
    assert 'ref' not in packet
    row = dict(segment_id='s', status='effective', rationale='Original claim')
    value = {'assessments': [
        dict(row, dimension=DIMENSIONS[0], evidence_refs=[packet['evidence_id']]),
        dict(row, dimension=DIMENSIONS[1], evidence_refs=[full]),
        dict(row, dimension=DIMENSIONS[2], evidence_refs=[]),
        dict(row, dimension=DIMENSIONS[3], status='unknown', rationale='No recovery opportunity', evidence_refs=[]),
    ]}
    rows = _validate_focus(value, review, ['s'])['assessments']
    assert rows[0]['status'] == 'effective'
    assert rows[0]['evidence_refs'] == [packet['returned_ref']]
    assert [r.get('unknown_reason') for r in rows[1:]] == [
        'citation_outside_delivered_ranges', 'missing_citation', 'evaluator_uncertain', 'missing_assessment']
    assert rows[1]['proposed_rationale'] == 'Original claim'
    with pytest.raises(ValueError, match='Unknown delivered evidence ID'):
        _validate_focus({'assessments': [dict(row, dimension=DIMENSIONS[0], evidence_refs=['e999'])]}, review, ['s'])


def test_focus_reads_actual_results_instead_of_repeated_envelopes(tmp_path):
    events = calls(1) + [
        {'type': 'tool.started', 'tool_id': 'shell_execute', 'tool_call_id': 't', 'input': {'command': 'calculate speed'}},
        {'type': 'tool.completed', 'tool_id': 'shell_execute', 'tool_call_id': 't', 'output': {'value': {
            'argv': ['large wrapper' * 1000], 'stdout': 'Earth: 29.78 km/s', 'exit_code': 0}}},
    ]
    store = source(tmp_path, events)
    segment = {'evidence_refs': [asdict(store.ref(e)) for e in store.events]}
    refs = focus_evidence_refs(store, segment)
    assert not any(store.event(r).event_type == 'llm.requested' for r in refs)
    assert 'response.content' in [r['field_path'] for r in refs]
    assert 'output.value.stdout' in [r['field_path'] for r in refs]
    assert 'Earth: 29.78 km/s' in [store.resolve(r) for r in refs]
    assert 'output.value.exit_code' in [r['field_path'] for r in refs]
    assert not any('large wrapper' in str(store.resolve(r)) for r in refs)


def test_supported_dimensions_without_diagnoses_survive_synthesis(tmp_path):
    class DimensionJudge(BehaviorJudge):
        async def chat(self, messages, tools=None, cancellation=None):
            result = await super().chat(messages, tools, cancellation)
            payload = json.loads(messages[1].content)
            if payload['stage'].startswith('focus:'):
                value = json.loads(result.value.content)
                value['diagnoses'] = []
                for row in value['assessments']:
                    row['evidence_refs'] = [payload['evidence_packets'][0]['evidence_id']]
                from dataclasses import replace

                from loom.core import ok
                return ok(replace(result.value, content=json.dumps(value)))
            if payload['stage'] == 'synthesis':
                assert len(payload['assessments']) == 5
                assert payload['coverage']['delivered_focus_packets'] > 0
                assert payload['evidence_packets']
            return result
    store = source(tmp_path, calls(2))
    facts = build_behavior_facts(store)
    result = asyncio.run(judge_behavior(store, facts, DimensionJudge())).unwrap()
    assert result['coverage']['supported_segments'] == 1
    assert not result['coverage']['unknown_reasons']
    assert result['evidence_packets']


def test_runtime_acceptance_does_not_disappear_without_independent_receipts(tmp_path):
    events = [{'type': 'run.started'}, {'type': 'task.goal.revised', 'objective': 'Check speed', 'goal_revision': 1},
              *calls(1), {'type': 'acceptance.gate.passed', 'acceptance': {'state': 'passed', 'results': [{'status': 'passed'}]}},
              {'type': 'run.completed'}]
    base = build_behavior_facts(source(tmp_path, events))['base']
    assert base['runtime_acceptance']['state']['state'] == 'passed'
    assert base['outcomes'][-1]['status'] == 'unverified'
    assert 'No independent verification receipts' in base['outcomes'][-1]['explanation']
